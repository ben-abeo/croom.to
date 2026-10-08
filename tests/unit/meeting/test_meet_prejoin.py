"""
The Google Meet provider against the guest pre-join page as Meet renders it in
October 2026 (seen on a room Pi): a "What's your name?" field whose only hint
is its placeholder, and an "Ask to join" control that stays disabled until a
name is typed. The pages here are replicas served locally.
"""

from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.providers.google_meet import GoogleMeetProvider  # noqa: E402
from croom.meeting.display import TvDisplay  # noqa: E402
from tests.unit.meeting.fake_display import FakeDisplay  # noqa: E402

# A <button disabled> with the text in a nested span, a name field with a placeholder only.
BUTTON_FORM = """
<!DOCTYPE html><html><body>
<h2>What's your name?</h2>
<input type="text" placeholder="Your name" maxlength="60">
<button disabled><span>Ask to join</span></button>
<div id="meeting" hidden><button aria-label="Leave call">Leave</button></div>
<script>
const input = document.querySelector('input');
const join = document.querySelector('button');
input.addEventListener('input', () => { join.disabled = !input.value.trim(); });
join.addEventListener('click', () => {
  if (join.disabled) return;
  setTimeout(() => { document.getElementById('meeting').hidden = false; }, 300);
});
</script>
</body></html>
"""

# A div with role=button and aria-disabled, a name field with an aria-label.
ROLE_FORM = """
<!DOCTYPE html><html><body>
<input type="text" aria-label="Your name">
<div role="button" aria-disabled="true" tabindex="0">Ask to join</div>
<div id="meeting" hidden><button aria-label="Leave call">Leave</button></div>
<script>
const input = document.querySelector('input');
const join = document.querySelector('[role=button]');
input.addEventListener('input', () => { join.setAttribute('aria-disabled', input.value.trim() ? 'false' : 'true'); });
join.addEventListener('click', () => {
  if (join.getAttribute('aria-disabled') === 'true') return;
  setTimeout(() => { document.getElementById('meeting').hidden = false; }, 300);
});
</script>
</body></html>
"""

REFUSAL_PAGE = """
<!DOCTYPE html><html><body>
<h1>You can't join this video call</h1>
<button>Return to home screen</button>
<p>Your meeting is safe. No one can join a meeting unless invited or admitted by the host.</p>
</body></html>
"""


async def drive(form, tmp_path, name="Room 1"):
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(form)
        provider = GoogleMeetProvider(FakeDisplay())
        provider._page = page
        provider.failure_screenshot = tmp_path / "meet-failure.png"
        try:
            await provider._handle_prejoin(name, True, True)
            typed = await page.input_value("input[type=text]")
            await provider._click_join_button()
            await page.wait_for_selector("#meeting:not([hidden])", timeout=5000)
            return typed
        finally:
            await browser.close()


async def test_types_the_room_name_into_a_placeholder_only_field_and_presses_the_button_once_it_enables(tmp_path):
    assert await drive(BUTTON_FORM, tmp_path) == "Room 1"


async def test_handles_a_role_button_that_is_disabled_by_aria(tmp_path):
    assert await drive(ROLE_FORM, tmp_path) == "Room 1"


async def test_failure_quotes_what_meet_showed_and_saves_a_screenshot(tmp_path):
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(REFUSAL_PAGE)
        provider = GoogleMeetProvider(FakeDisplay())
        provider._page = page
        provider.failure_screenshot = tmp_path / "meet-failure.png"
        provider.JOIN_FIND_TIMEOUT_MS = 300
        try:
            with pytest.raises(RuntimeError) as failure:
                await provider._click_join_button()
            message = str(failure.value)
            assert "You can't join this video call" in message
            assert str(tmp_path / "meet-failure.png") in message
            assert (tmp_path / "meet-failure.png").stat().st_size > 0
        finally:
            await browser.close()


def test_browser_identifies_as_itself():
    """Meet refuses browsers it deems too old; the context must not claim an older Chrome."""
    options = TvDisplay.context_options()
    assert "user_agent" not in options
    assert options["permissions"] == ["camera", "microphone"]
    assert options["no_viewport"] is True   # the page fills the TV, whatever its resolution

# A signed-in pre-join page: no name field, "Join now", a muted microphone and a camera that is on.
SIGNED_IN_FORM = """
<!DOCTYPE html><html><body>
<h2>Ready to join?</h2>
<button id="mic" aria-label="Turn on microphone (ctrl + d)">mic_off</button>
<button id="cam" aria-label="Turn off camera (ctrl + e)">videocam</button>
<button id="join">Join now</button>
<div id="meeting" hidden><button aria-label="Leave call">Leave</button></div>
<script>
const flip = (btn, device) => btn && btn.addEventListener('click', () => {
  const on = btn.getAttribute('aria-label').startsWith('Turn off');
  btn.setAttribute('aria-label', (on ? 'Turn on ' : 'Turn off ') + device);
  btn.dataset.clicks = String(Number(btn.dataset.clicks || 0) + 1);
});
flip(document.getElementById('mic'), 'microphone (ctrl + d)');
flip(document.getElementById('cam'), 'camera (ctrl + e)');
document.getElementById('join').addEventListener('click', () => {
  setTimeout(() => { document.getElementById('meeting').hidden = false; }, 300);
});
</script>
</body></html>
"""

MIC_ONLY_FORM = SIGNED_IN_FORM.replace('<button id="cam" aria-label="Turn off camera (ctrl + e)">videocam</button>', "")

SIGN_IN_PAGE = """
<!DOCTYPE html><html><body>
<h1>Sign in</h1>
<p>to continue to Google Meet</p>
<input type="email" aria-label="Email or phone">
<button>Next</button>
</body></html>
"""

LOBBY_PAGE = """
<!DOCTYPE html><html><body>
<p>Asking to join…</p>
<p>You'll join the call when someone lets you in</p>
<div id="meeting" hidden><button aria-label="Leave call">Leave</button></div>
<script>setTimeout(() => { document.getElementById('meeting').hidden = false; }, 700);</script>
</body></html>
"""


async def signed_in(form, tmp_path, camera_on, mic_on):
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(form)
        provider = GoogleMeetProvider(FakeDisplay(profile_dir=str(tmp_path / "profile")))
        provider._page = page
        provider.failure_screenshot = tmp_path / "meet-failure.png"
        try:
            await provider._handle_prejoin("Room 3", camera_on, mic_on)
            await provider._click_join_button()
            await page.wait_for_selector("#meeting:not([hidden])", timeout=5000)
            labels = await page.evaluate("() => [...document.querySelectorAll('#mic,#cam')].map(b => [b.id, b.getAttribute('aria-label'), b.dataset.clicks || '0'])")
            return {row[0]: (row[1], row[2]) for row in labels}
        finally:
            await browser.close()


async def test_turns_the_microphone_on_and_leaves_the_camera_alone_when_both_are_wanted_on(tmp_path):
    state = await signed_in(SIGNED_IN_FORM, tmp_path, camera_on=True, mic_on=True)
    assert state["mic"] == ("Turn off microphone (ctrl + d)", "1")
    assert state["cam"] == ("Turn off camera (ctrl + e)", "0")


async def test_turns_the_camera_off_and_leaves_the_microphone_alone_when_both_are_wanted_off(tmp_path):
    state = await signed_in(SIGNED_IN_FORM, tmp_path, camera_on=False, mic_on=False)
    assert state["cam"] == ("Turn on camera (ctrl + e)", "1")
    assert state["mic"] == ("Turn on microphone (ctrl + d)", "0")


async def test_a_missing_toggle_does_not_stop_the_join(tmp_path):
    state = await signed_in(MIC_ONLY_FORM, tmp_path, camera_on=True, mic_on=True)
    assert state["mic"] == ("Turn off microphone (ctrl + d)", "1")
    assert "cam" not in state


async def test_googles_sign_in_page_names_the_sign_in_command(tmp_path):
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(SIGN_IN_PAGE)
        provider = GoogleMeetProvider(FakeDisplay(profile_dir=str(tmp_path / "profile")))
        provider._page = page
        provider.JOIN_FIND_TIMEOUT_MS = 300
        provider.TOGGLE_TIMEOUT_MS = 300
        try:
            with pytest.raises(RuntimeError) as failure:
                await provider._click_join_button()
            assert "croom --sign-in-meet" in str(failure.value)
            assert "expired or was never done" in str(failure.value)
        finally:
            await browser.close()


async def test_waiting_to_be_admitted_is_the_lobby(tmp_path):
    from croom.meeting.providers.base import MeetingState
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(LOBBY_PAGE)
        provider = GoogleMeetProvider(FakeDisplay())
        provider._page = page
        provider.CONNECT_TIMEOUT_MS = 300
        provider.ADMIT_TIMEOUT_MS = 5000
        try:
            await provider._wait_for_connection()
            assert provider._state == MeetingState.IN_LOBBY
        finally:
            await browser.close()


COVERED_TOGGLE_FORM = SIGNED_IN_FORM.replace(
    "</body>",
    '<div style="position:fixed;inset:0;background:rgba(0,0,0,.01);z-index:9"></div>'
    '<script>document.getElementById("join").style.zIndex="10";document.getElementById("join").style.position="relative";</script></body>',
)


async def test_a_toggle_that_cannot_be_clicked_does_not_stop_the_join(tmp_path):
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(COVERED_TOGGLE_FORM)
        provider = GoogleMeetProvider(FakeDisplay(profile_dir=str(tmp_path / "profile")))
        provider._page = page
        provider.TOGGLE_TIMEOUT_MS = 500
        try:
            await provider._handle_prejoin("Room 3", True, True)
            await provider._click_join_button()
            await page.wait_for_selector("#meeting:not([hidden])", timeout=5000)
        finally:
            await browser.close()
