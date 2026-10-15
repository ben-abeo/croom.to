"""
The Google Meet provider's controls against a replica of Meet's pages (spec 2026-10-13, section 4.5): the guest pre-join page, then a call
with the buttons Meet labels for the room and the keyboard shortcuts it answers (Ctrl+D, Ctrl+E, Ctrl+Alt+H, and C on its own). The replica
is served on this machine's loopback to the test's own browser: the provider's navigation to meet.google.com is sent there, and the browser
refuses every other address, so nothing leaves the machine and no real Meet page is ever loaded.
"""

import asyncio
import copy
import logging
import re
import time
from urllib.parse import urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.providers.base import MeetingControlError, MeetingState  # noqa: E402
from croom.meeting.providers.google_meet import GoogleMeetProvider  # noqa: E402
from tests.unit.meeting.fake_display import FakeDisplay  # noqa: E402

# A guest's pre-join page: a name field, the microphone (on) and the camera (off) the room asked for, and the button that joins.
PREJOIN = """
<div id="prejoin">
  <h2>What's your name?</h2>
  <input aria-label="Your name" placeholder="Your name" maxlength="60">
  <button aria-label="Turn off microphone (ctrl + d)">mic</button>
  <button aria-label="Turn on camera (ctrl + e)">cam</button>
  <button id="join"><span>Ask to join</span></button>
</div>
"""

# The call's buttons, worded as Meet words them: the label says what pressing the button does, so "Turn off microphone" means the microphone
# is live and "Lower hand" means the hand is up. The room's microphone is on, its camera off, its hand down and the captions off; three
# people are in the call.
BUTTONS = """
  <button aria-label="Turn off microphone (ctrl + d)">mic</button>
  <button aria-label="Turn on camera (ctrl + e)">cam</button>
  <button aria-label="Raise hand (ctrl + alt + h)">hand</button>
  <button aria-label="Turn on captions (c)">cc</button>
  <button id="people" aria-label="Show everyone (3)">people</button>
  <button aria-label="Leave call">Leave</button>
"""
CAPTIONS = '<button aria-label="Turn on captions (c)">cc</button>'
HAND = '<button aria-label="Raise hand (ctrl + alt + h)">hand</button>'
PEOPLE = '<button id="people" aria-label="Show everyone (3)">people</button>'
CHAT_BOX = '<input id="chat" aria-label="Send a message">'

# Meet's keyboard: each shortcut flips its button's label (after window.__delay milliseconds, when set), a key in window.__ignore is one Meet does
# not take just now, and the bare C goes into a text field when one has the keyboard, as it does in Meet. window.__keys is what the page was sent.
SCRIPT = """
<script>
const by = (s) => document.querySelector(s);
const flip = (b, a, c) => b && setTimeout(() => b.setAttribute('aria-label', b.getAttribute('aria-label').startsWith(a) ? c : a), window.__delay || 0);
window.__keys = [];
document.addEventListener('keydown', (e) => {
  if (['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) return;
  window.__keys.push((e.ctrlKey ? 'Control+' : '') + (e.altKey ? 'Alt+' : '') + e.key);
  if ((window.__ignore || []).includes(e.key)) return;
  if (e.ctrlKey && e.key === 'd') flip(by('[aria-label*="microphone"]'), 'Turn off microphone (ctrl + d)', 'Turn on microphone (ctrl + d)');
  if (e.ctrlKey && e.key === 'e') flip(by('[aria-label*="camera"]'), 'Turn off camera (ctrl + e)', 'Turn on camera (ctrl + e)');
  if (e.ctrlKey && e.altKey && e.key === 'h') flip(by('[aria-label*="hand"]'), 'Raise hand (ctrl + alt + h)', 'Lower hand (ctrl + alt + h)');
  if (!e.ctrlKey && e.key === 'c' && !/^(INPUT|TEXTAREA)$/.test(e.target.tagName)) flip(by('[aria-label*="captions"]'), 'Turn on captions (c)', 'Turn off captions (c)');
});
document.getElementById('join').addEventListener('click', () => {
  setTimeout(() => { document.getElementById('prejoin').remove(); document.getElementById('meeting').hidden = false; }, 100);
});
</script>
"""


def replica(buttons=BUTTONS):
    """Meet's pre-join page, which becomes the call (the pre-join page gone, as Meet's is) when Join is pressed."""
    return ("<!DOCTYPE html><html><body>" + PREJOIN + '<div id="meeting" hidden>' + buttons + CHAT_BOX + "</div>" + SCRIPT + "</body></html>")


@pytest.fixture
async def meet_page():
    """A factory: `await meet_page(html)` serves the replica on loopback, joins it with a provider and gives back (provider, info) with the
    call connected (a guest, whose microphone is on and whose camera is off unless the join asks for it on). The provider reads the page
    every `read_interval` seconds (an hour: the readings the tests look at are the ones they ask for); `setup(provider)` runs before the
    join."""
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        servers, providers, refused = [], [], []

        async def join(html, read_interval=3600, setup=None, camera_on=False):
            async def serve(request):
                return web.Response(text=html, content_type="text/html")

            app = web.Application()
            app.router.add_get("/{code}", serve)
            server = TestServer(app)          # 127.0.0.1, a free port
            await server.start_server()
            servers.append(server)
            page = await browser.new_page()

            async def loopback_only(route):
                if urlsplit(route.request.url).hostname == "127.0.0.1":
                    await route.continue_()
                else:
                    refused.append(route.request.url)
                    await route.abort()

            await page.route("**/*", loopback_only)
            real_goto = page.goto

            async def goto(url, **options):
                meet = re.fullmatch(r"https://meet\.google\.com/([a-z-]+)", url)
                return await real_goto(f"http://127.0.0.1:{server.port}/{meet.group(1)}" if meet else url, **options)

            page.goto = goto                  # the provider asks for meet.google.com; this browser is sent to the replica instead
            provider = GoogleMeetProvider(FakeDisplay(page))
            provider.READ_INTERVAL_S = read_interval
            providers.append(provider)
            if setup is not None:
                setup(provider)
            await provider.initialize()
            info = await provider.join_meeting("abc-defg-hij", display_name="Room 1", camera_on=camera_on, mic_on=True)
            return provider, info

        try:
            yield join
        finally:
            for provider in providers:
                await provider.shutdown()
            for server in servers:
                await server.close()
            await browser.close()
        assert refused == [], f"the page asked for an address off this machine: {refused}"


async def keys(provider):
    """The shortcuts the replica was sent, in order."""
    return await provider._page.evaluate("window.__keys")


async def set_label(provider, containing, label):
    """The page changes a button's label on its own (the host muted the room): the provider was not asked, and has not looked since."""
    await provider._page.evaluate(
        "([part, label]) => document.querySelector('[aria-label*=\"' + part + '\"]').setAttribute('aria-label', label)", [containing, label])


async def eventually(check, seconds=3.0):
    """Wait, briefly, for `check` to come true, and say whether it did."""
    end = time.monotonic() + seconds
    while True:
        if check():
            return True
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(0.02)


class Reads:
    """Counts the reads of Meet's buttons the provider makes, whoever asks for them."""

    def __init__(self, provider):
        self.count = 0
        real = provider._read_controls

        async def counted():
            self.count += 1
            return await real()

        provider._read_controls = counted


# ---- The model Meet reports, and the shortcuts that press its buttons ----


async def test_meet_reports_its_buttons_as_controls_and_presses_them_with_shortcuts(meet_page):
    provider, info = await meet_page(replica())
    assert provider.state == MeetingState.CONNECTED
    assert info.controls["camera"] == {"available": True, "on": False} and info.controls["captions"] == {"available": True, "on": False}
    assert info.controls["raise_hand"] == {"available": True, "on": False} and info.controls["host"]["role"] == "participant"
    assert info.controls["reactions"]["available"] is False and info.people["count"] == 3
    state = await provider.control("camera")
    assert state["camera"]["on"] is True and info.is_camera_on is True
    state = await provider.control("raise_hand")
    assert state["raise_hand"]["on"] is True
    state = await provider.control("captions")
    assert state["captions"]["on"] is True
    with pytest.raises(MeetingControlError):
        await provider.control("host.mute_all")
    assert await keys(provider) == ["Control+e", "Control+Alt+h", "c"]      # Meet's own shortcuts, and nothing else was pressed


async def test_the_model_is_what_meet_offers_a_participant_and_no_more(meet_page):
    provider, info = await meet_page(replica(), camera_on=True)      # the join asked for the camera on; the page says it is off
    controls = info.controls
    assert controls["mute"] == {"available": True, "on": True}              # the microphone is live: its label says "Turn off"
    assert info.is_muted is False and info.is_camera_on is False            # the buttons' own words, not what the join asked for
    assert controls["leave"] == {"available": True}
    assert controls["chat"] == {"available": False} and info.chat == {"unread": 0, "messages": []}   # the agent does not read Meet's chat
    assert controls["reactions"] == {"available": False}
    assert [name for name in ("layout", "hide_self", "pin") if controls[name]["available"]] == []
    host = controls["host"]
    assert host["role"] == "participant"
    assert [name for name, item in host.items() if name != "role" and item["available"]] == []
    assert info.people == {"count": 3, "list": []} and info.recording is False and info.locked is False


async def test_the_model_is_in_place_when_the_call_is_announced_connected(meet_page):
    """A status that says connected before the buttons were read would draw the three buttons of a platform with no model, and then the rest."""
    seen = []

    def watch(provider):
        provider.add_state_callback(lambda state: seen.append((state, provider.current_meeting and copy.deepcopy(provider.current_meeting.controls))))

    await meet_page(replica(), setup=watch)
    connected = [controls for state, controls in seen if state == MeetingState.CONNECTED]
    assert len(connected) == 1 and connected[0]["camera"] == {"available": True, "on": False}


async def test_a_button_the_page_does_not_show_is_unavailable_and_asking_for_it_refuses_without_pressing(meet_page):
    provider, info = await meet_page(replica(BUTTONS.replace(CAPTIONS, "").replace(HAND, "").replace(PEOPLE, "")))
    assert info.controls["captions"] == {"available": False} and info.controls["raise_hand"] == {"available": False}
    assert info.controls["camera"] == {"available": True, "on": False}
    assert info.people == {"count": 0, "list": []}
    with pytest.raises(MeetingControlError, match="^Meet shows no captions button right now$"):
        await provider.control("captions")
    with pytest.raises(MeetingControlError, match="^Meet shows no raise hand button right now$"):
        await provider.control("raise_hand")
    assert await keys(provider) == []                                         # a key that has no button to answer it is not pressed


async def test_a_button_whose_label_does_not_say_its_state_is_not_offered(meet_page):
    """Without the label's state a press cannot be confirmed, and the table never guesses: the camera button that says only "Camera" is unavailable."""
    provider, info = await meet_page(replica(BUTTONS.replace("Turn on camera (ctrl + e)", "Camera")))
    assert info.controls["camera"] == {"available": False} and info.is_camera_on is False      # the last state known (the join's) stays
    with pytest.raises(MeetingControlError, match="^Meet shows no camera button right now$"):
        await provider.control("camera")


async def test_the_people_count_is_the_number_in_the_label_or_the_text_and_zero_without_one(meet_page):
    provider, info = await meet_page(replica())
    page = provider._page

    async def people(html):
        await page.evaluate("(html) => { const b = document.getElementById('people'); b.outerHTML = html; }", html)
        await provider._read_controls()
        return info.people

    assert await people('<button id="people" aria-label="Show everyone">people<span>12</span></button>') == {"count": 12, "list": []}   # in the text
    assert await people('<button id="people" aria-label="Show everyone (7)">people</button>') == {"count": 7, "list": []}                   # in the label
    assert await people('<button id="people" aria-label="Participants">people</button>') == {"count": 0, "list": []}                         # no number
    assert await people('<button id="people" aria-label="People">people<span>5</span></button>') == {"count": 5, "list": []}                # the other wording
    assert await people('<button id="people" aria-label="Add people">add<span>9</span></button>') == {"count": 0, "list": []}               # not the people button
    assert await people('<button id="people" aria-label="Show everyone (3)">people</button>') == {"count": 3, "list": []}
    assert await people("<span id=\"people\"></span>") == {"count": 0, "list": []}                                                          # no button


@pytest.mark.parametrize("name, value", [
    ("reactions", "Clap"), ("layout", "gallery"), ("hide_self", None), ("pin", None), ("dialog", {"id": 1, "button": "OK"}),
    ("host.mute_all", None), ("host.record", None), ("host.end", None), ("host.mute_one", "5"),
])
async def test_what_meet_does_not_offer_is_refused_in_words_and_nothing_is_pressed(meet_page, name, value):
    provider, info = await meet_page(replica())
    with pytest.raises(MeetingControlError, match="^Not available in a Meet call$"):
        await provider.control(name, value)
    assert await keys(provider) == []


async def test_chat_seen_has_nothing_to_mark_and_answers_with_the_chat(meet_page):
    provider, info = await meet_page(replica())
    assert await provider.control("chat.seen") == {"chat": {"unread": 0, "messages": []}}
    assert await keys(provider) == []


async def test_a_control_with_no_call_is_refused_in_words():
    with pytest.raises(MeetingControlError, match="^Not in a meeting$"):
        await GoogleMeetProvider(FakeDisplay()).control("camera")


async def test_a_control_after_the_call_is_over_is_refused_in_words(meet_page):
    provider, info = await meet_page(replica())
    await provider.leave_meeting()
    with pytest.raises(MeetingControlError, match="^Not in a meeting$"):
        await provider.control("camera")


# ---- A press is confirmed by the label, never assumed ----


@pytest.mark.parametrize("name, key, words", [("mute", "d", "microphone"), ("camera", "e", "camera"), ("raise_hand", "h", "raise hand"), ("captions", "c", "captions")])
async def test_a_press_whose_label_does_not_change_is_a_refusal_in_words_and_changes_nothing(meet_page, name, key, words):
    provider, info = await meet_page(replica())
    provider.PRESS_WAIT_S = 0.3
    await provider._page.evaluate("(key) => { window.__ignore = [key]; }", key)           # Meet takes no such shortcut just now
    before = copy.deepcopy(info.controls)
    with pytest.raises(MeetingControlError, match=f"^Meet did not change the {words}; try again$"):
        await provider.control(name)
    assert info.controls == before and info.is_muted is False and info.is_camera_on is False


async def test_a_label_that_changes_late_is_waited_for_and_the_press_took(meet_page):
    """Not a fixed half second: the label is looked at until it has changed (for two seconds at most)."""
    provider, info = await meet_page(replica())
    await provider._page.evaluate("window.__delay = 1200")
    state = await provider.control("camera")
    assert state["camera"]["on"] is True and info.is_camera_on is True


async def test_the_press_and_the_reading_have_their_bounds_pinned():
    assert GoogleMeetProvider.READ_INTERVAL_S == 5
    assert GoogleMeetProvider.PRESS_WAIT_S == 2 and GoogleMeetProvider.PRESS_POLL_S == 0.05


@pytest.mark.parametrize("toggle, attribute, part, page_sets", [
    ("toggle_mute", "is_muted", "microphone", "Turn on microphone (ctrl + d)"),      # the host muted the room, and the provider still has it live
    ("toggle_camera", "is_camera_on", "camera", "Turn off camera (ctrl + e)"),        # the camera was put on at the TV, and the provider still has it off
])
async def test_toggling_reports_the_state_the_label_says_and_not_the_flip_of_what_was_last_known(meet_page, toggle, attribute, part, page_sets):
    """The page changed the button under the provider, which has not looked since: the key turns it the other way (the room is live again, the
    camera is off again) and the answer is what the label says now. True is muted for the microphone and on for the camera."""
    provider, info = await meet_page(replica())
    await set_label(provider, part, page_sets)
    assert await getattr(provider, toggle)() is False
    assert getattr(info, attribute) is False                 # the model says what the button says, as the answer did
    assert await getattr(provider, toggle)() is True
    assert getattr(info, attribute) is True


async def test_the_mute_control_goes_through_toggle_mute(meet_page):
    provider, info = await meet_page(replica())
    calls = []
    real = provider.toggle_mute

    async def spy():
        calls.append("toggle_mute")
        return await real()

    provider.toggle_mute = spy
    state = await provider.control("mute")
    assert calls == ["toggle_mute"] and info.is_muted is True
    assert state["mute"] == {"available": True, "on": False}          # the microphone is not live: the table offers Unmute
    assert await keys(provider) == ["Control+d"]


# ---- The bare c key belongs to the page, not to a text field ----


async def test_the_bare_c_key_is_never_pressed_into_a_text_field(meet_page):
    provider, info = await meet_page(replica())
    page = provider._page
    await page.focus("#chat")                              # the chat box has the keyboard
    with pytest.raises(MeetingControlError, match="^Meet's chat box has the keyboard; close it on the TV first$"):
        await provider.control("captions")
    assert await page.input_value("#chat") == "" and await keys(provider) == []
    assert info.controls["captions"] == {"available": True, "on": False}
    state = await provider.control("camera")               # a shortcut with Ctrl is not typed into the field: pressed as ever
    assert state["camera"]["on"] is True
    await page.evaluate("document.activeElement.blur()")
    state = await provider.control("captions")             # nothing has the keyboard
    assert state["captions"]["on"] is True
    await page.evaluate("document.querySelector('[aria-label=\"Turn off captions (c)\"]').focus()")
    state = await provider.control("captions")             # a button has it: the key reaches the page
    assert state["captions"]["on"] is False
    assert await page.input_value("#chat") == "" and await keys(provider) == ["Control+e", "c", "c"]


async def test_the_bare_c_key_goes_to_the_page_or_to_a_button_and_to_nothing_else(meet_page):
    """Whatever else has the keyboard is not the page's: a text field, an editable area, a link, something that is only focusable. A button
    (a real one, or an element that says it is one) passes the key on to the page."""
    provider, info = await meet_page(replica())
    page = provider._page
    await page.evaluate("""() => {
      const add = (html) => document.getElementById('meeting').insertAdjacentHTML('beforeend', html);
      add('<div id="note" contenteditable="true"></div><textarea id="area"></textarea><a id="link" href="#x">link</a>');
      add('<div id="choice" role="combobox" tabindex="0"></div><div id="plain" tabindex="0"></div><div id="press" role="button" tabindex="0"></div>');
    }""")
    for held in ("#note", "#area", "#link", "#choice", "#plain"):
        await page.focus(held)
        with pytest.raises(MeetingControlError, match="chat box has the keyboard"):
            await provider.control("captions")
    assert await keys(provider) == []
    await page.focus("#press")                             # a div that is a button
    state = await provider.control("captions")
    assert state["captions"]["on"] is True and await keys(provider) == ["c"]


# ---- One press at a time ----


async def test_two_controls_from_the_table_at_once_are_pressed_one_after_the_other(meet_page):
    """A press is a key down and a key up: two at once could meet in the middle (Ctrl still down for the C that follows)."""
    provider, info = await meet_page(replica())
    page = provider._page
    real = page.keyboard.press
    active = {"now": 0, "most": 0}

    async def slow_press(key, **options):
        active["now"] += 1
        active["most"] = max(active["most"], active["now"])
        try:
            await asyncio.sleep(0.15)                      # long enough for the other press to have come
            return await real(key, **options)
        finally:
            active["now"] -= 1

    page.keyboard.press = slow_press
    await asyncio.gather(provider.control("camera"), provider.control("captions"))
    assert active["most"] == 1
    assert info.controls["camera"]["on"] is True and info.controls["captions"]["on"] is True


# ---- Reading Meet's buttons on a schedule, for as long as the call lasts ----


async def test_the_model_is_read_again_while_connected_so_what_the_host_does_shows(meet_page):
    provider, info = await meet_page(replica(), read_interval=0.05)
    assert info.is_muted is False
    await set_label(provider, "microphone", "Turn on microphone (ctrl + d)")                  # the host mutes the room; nobody asked the provider
    assert await eventually(lambda: info.is_muted is True)
    assert info.controls["mute"] == {"available": True, "on": False}
    await provider._page.evaluate("document.getElementById('people').setAttribute('aria-label', 'Show everyone (4)')")
    assert await eventually(lambda: info.people["count"] == 4)


async def test_the_reader_stops_with_the_call(meet_page):
    provider, info = await meet_page(replica(), read_interval=0.05)
    reads = Reads(provider)
    assert await eventually(lambda: reads.count >= 3)                                          # it is reading
    reader = provider._reader
    await provider.leave_meeting()
    assert reader.done() and provider._reader is None                                          # cancelled, and waited for
    seen = reads.count
    await asyncio.sleep(0.4)
    assert reads.count == seen                                                                 # and no read runs after the call is over


async def test_shutting_down_stops_the_reader_too(meet_page):
    provider, info = await meet_page(replica(), read_interval=0.05)
    reads = Reads(provider)
    assert await eventually(lambda: reads.count >= 2)
    reader = provider._reader
    await provider.shutdown()
    assert reader.done() and provider._reader is None
    seen = reads.count
    await asyncio.sleep(0.3)
    assert reads.count == seen


async def test_stopping_the_reader_waits_for_it_to_be_over(meet_page):
    """Cancelled and awaited: whoever stops it can go on knowing no read is still on its way to the page."""
    provider, info = await meet_page(replica(), read_interval=0.05)
    reader = provider._reader
    await provider._stop_reading()
    assert reader.cancelled() and provider._reader is None


async def test_joining_again_leaves_one_reader_behind_the_call_and_none_after_it(meet_page):
    """The first call's reader is asleep (an hour) through the whole second join: only a join that stops it will end it."""
    provider, info = await meet_page(replica())
    first = provider._reader
    await provider.join_meeting("abc-defg-hij", display_name="Room 1", camera_on=False, mic_on=True)
    second = provider._reader
    assert first.cancelled() and second is not first and not second.done()
    await provider.leave_meeting()
    assert second.cancelled() and provider._reader is None


async def test_a_read_that_fails_is_one_warning_for_the_call_and_the_last_model_stays(meet_page, caplog):
    provider, info = await meet_page(replica(), read_interval=0.05)
    reads = Reads(provider)
    last = copy.deepcopy(info.controls)
    page = provider._page
    real = page.evaluate

    async def gone(*args, **kwargs):
        raise RuntimeError("Page.evaluate: Execution context was destroyed, most likely because of a navigation\nCall log: ...")

    def said():
        return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "Meet's buttons" in r.getMessage()]

    caplog.set_level(logging.DEBUG, logger="croom.meeting.providers.google_meet")
    page.evaluate = gone                                                                       # the page is gone from under the reader
    mark = reads.count
    assert await eventually(lambda: reads.count >= mark + 3)
    assert len(said()) == 1 and "Execution context was destroyed" in said()[0] and "Call log" not in said()[0]
    assert info.controls == last and info.people == {"count": 3, "list": []}
    page.evaluate = real                                                                       # the page is back: the reader went on
    await set_label(provider, "microphone", "Turn on microphone (ctrl + d)")
    assert await eventually(lambda: info.is_muted is True)
    # The next call has its own one warning: it is the call's, not the provider's.
    await provider.join_meeting("abc-defg-hij", display_name="Room 1", camera_on=False, mic_on=True)
    again = Reads(provider)
    page.evaluate = gone
    mark = again.count
    assert await eventually(lambda: again.count >= mark + 3)
    assert len(said()) == 2


async def test_a_press_that_cannot_reach_the_page_is_a_refusal_in_words(meet_page):
    provider, info = await meet_page(replica())
    page = provider._page

    async def gone(*args, **kwargs):
        raise RuntimeError("Page.evaluate: Target page, context or browser has been closed")

    page.evaluate = gone
    with pytest.raises(MeetingControlError, match="^Could not change the camera: Page.evaluate: Target page, context or browser has been closed$"):
        await provider.control("camera")
