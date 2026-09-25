"""
The Zoom provider against the web client's current pre-join form (web client
7.x, September 2026): a "Your Name" field with id input-for-name and no
placeholder, and a Join button that is disabled by CSS class, not attribute,
until a name is typed. The form here is a replica served locally.
"""

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.providers.zoom import ZoomProvider  # noqa: E402

CURRENT_FORM = """
<!DOCTYPE html><html><body>
<h1>Enter Meeting Info</h1>
<label for="input-for-name">Your Name</label>
<input id="input-for-name" type="text">
<label><input type="checkbox"> Remember my name for future meetings</label>
<button class="zm-btn preview-join-button disabled zm-btn__outline--blue zm-btn--disabled">Join</button>
<div id="meeting" hidden><button aria-label="Leave">Leave</button></div>
<script>
const input = document.getElementById('input-for-name');
const join = document.querySelector('.preview-join-button');
input.addEventListener('input', () => {
  if (input.value.trim()) join.classList.remove('disabled', 'zm-btn--disabled');
  else join.classList.add('disabled', 'zm-btn--disabled');
});
join.addEventListener('click', () => {
  if (join.classList.contains('disabled')) return;  // the web client ignores clicks while disabled
  setTimeout(() => { document.getElementById('meeting').hidden = false; }, 300);
});
</script>
</body></html>
"""

LEGACY_FORM = """
<!DOCTYPE html><html><body>
<input id="inputname" type="text" placeholder="Your Name">
<button id="joinBtn">Join</button>
<div id="meeting" hidden><button class="leave-btn">Leave</button></div>
<script>
document.getElementById('joinBtn').addEventListener('click', () => {
  if (!document.getElementById('inputname').value) return;
  document.getElementById('meeting').hidden = false;
});
</script>
</body></html>
"""


async def drive(form, name="Room 1"):
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(form)
        provider = ZoomProvider()
        provider._page = page
        try:
            await provider._handle_prejoin(name, True, True)
            typed = await page.input_value("input[type=text]")
            await provider._click_join_button()
            await page.wait_for_selector("#meeting:not([hidden])", timeout=5000)
            return typed
        finally:
            await browser.close()


async def test_types_the_room_name_and_presses_join_once_it_enables():
    assert await drive(CURRENT_FORM) == "Room 1"


async def test_still_handles_the_older_form():
    assert await drive(LEGACY_FORM) == "Room 1"


BLOCKED_PAGE = """
<!DOCTYPE html><html><body>
<h1>Enter Meeting Info</h1>
<p>Automated bots aren't allowed to join this meeting. If this was a mistake and you are a human, sign in to join the meeting.</p>
<button>Sign in to join</button>
</body></html>
"""


async def test_failure_message_quotes_what_zoom_showed():
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(BLOCKED_PAGE)
        provider = ZoomProvider()
        provider._page = page
        provider.CONNECT_TIMEOUT_MS = 500
        try:
            with pytest.raises(RuntimeError) as failure:
                await provider._wait_for_connection()
            assert "Automated bots aren't allowed to join this meeting" in str(failure.value)
        finally:
            await browser.close()
