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
        provider = GoogleMeetProvider()
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
        provider = GoogleMeetProvider()
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
    options = GoogleMeetProvider.context_options()
    assert "user_agent" not in options
    assert options["permissions"] == ["camera", "microphone"]
    assert options["viewport"] == {"width": 1920, "height": 1080}
