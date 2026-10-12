"""
`croom --check-meet URL`: open a Google Meet link the way the room's provider
does and say what Meet rendered, so a failed join can be diagnosed from a
terminal instead of by watching the TV. Exit 0 when a join control was found,
1 otherwise.
"""

import asyncio
import sys
from pathlib import Path
from typing import Optional, TextIO

from croom.meeting.browser_env import ensure_browsers_path, profile_holder
from croom.meeting.display import TvDisplay
from croom.meeting.providers.google_meet import GoogleMeetProvider

DESCRIBE_JS = """
() => {
  const describe = (el) => ({
    tag: el.tagName.toLowerCase(), role: el.getAttribute('role'),
    text: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 40),
    disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
    ariaLabel: el.getAttribute('aria-label'), placeholder: el.getAttribute('placeholder'),
  });
  const inputs = [...document.querySelectorAll('input[type="text"], input:not([type])')].map(describe);
  const joins = [...document.querySelectorAll('button, [role="button"]')]
    .filter(el => /ask to join|join now/i.test(el.innerText || el.getAttribute('aria-label') || ''))
    .map(describe);
  const words = (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim().slice(0, 300);
  return { inputs, joins, words };
}
"""

DEVICES_JS = """
async () => {
  try { const s = await navigator.mediaDevices.getUserMedia({audio: true, video: true}); s.getTracks().forEach(t => t.stop()); } catch (e) {}
  const all = await navigator.mediaDevices.enumerateDevices();
  const names = (kind) => all.filter(d => d.kind === kind && d.label).map(d => d.label);
  return { microphones: names('audioinput'), speakers: names('audiooutput'), cameras: names('videoinput') };
}
"""
# An unresponsive Pulse socket can leave getUserMedia or enumerateDevices pending for good; the check must not wait on it.
DEVICES_TIMEOUT_S = 10


def resolve_profile(explicit: Optional[str], configured: str) -> Optional[Path]:
    """The profile the check opens: an explicit --profile wins, then the config's, else a guest."""
    if explicit:
        return Path(explicit)
    return Path(configured) if configured else None


async def _count(page, selectors) -> int:
    found = 0
    for selector in selectors:
        try:
            if await page.locator(selector).count():
                found += 1
        except Exception:
            pass
    return found


async def check_meet(url: str, out: TextIO = sys.stdout, screenshot: Optional[Path] = None,
                     headless: bool = False, settle_ms: int = 8000, profile: Optional[Path] = None) -> int:
    ensure_browsers_path()
    if profile is not None and profile_holder(profile) is not None:
        print(f"The room service is using {profile}; stop it first: sudo systemctl stop croom, "
              "or pass --profile DIR to check another profile", file=out)
        return 1
    from playwright.async_api import async_playwright

    if profile is not None:
        print(f"Opening {url} from the browser profile at {profile}, the way a signed-in room would", file=out)
        print(f"Profile: {profile}", file=out)
    else:
        print(f"Opening {url} as a guest, the way the room does", file=out)
    async with async_playwright() as p:
        if profile is not None:
            profile.mkdir(parents=True, exist_ok=True)
            browser = None
            context = await p.chromium.launch_persistent_context(
                str(profile), headless=headless, args=TvDisplay.BASE_ARGS,
                **TvDisplay.context_options())
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            browser = await p.chromium.launch(headless=headless, args=TvDisplay.BASE_ARGS)
            context = await browser.new_context(**TvDisplay.context_options())
            page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(settle_ms)
            nothing = {"microphones": [], "speakers": [], "cameras": []}
            try:
                devices = await asyncio.wait_for(page.evaluate(DEVICES_JS), DEVICES_TIMEOUT_S)
            except asyncio.TimeoutError:
                devices = {**nothing, "error": f"timed out after {DEVICES_TIMEOUT_S} s"}
            except Exception as e:  # noqa: BLE001 - a page without media APIs still gets the rest of the report
                devices = {**nothing, "error": str(e) or type(e).__name__}

            def listed(names):
                return ", ".join(names) if names else "none"

            print(f"Devices the browser sees: microphones: {listed(devices.get('microphones'))}; "
                  f"speakers: {listed(devices.get('speakers'))}; cameras: {listed(devices.get('cameras'))}", file=out)
            failure = str(devices.get("error") or "").strip()
            if failure:   # a failed listing reads differently from an empty one: say why
                print(f"Listing the devices failed: {failure.splitlines()[0]}", file=out)
            seen = await page.evaluate(DESCRIBE_JS)
            if seen["inputs"]:
                field = seen["inputs"][0]
                print(f"Name field: found (aria-label {field['ariaLabel']!r}, placeholder {field['placeholder']!r}); "
                      f"the room's selectors match it: {'yes' if await _count(page, GoogleMeetProvider.NAME_SELECTORS) else 'NO'}", file=out)
                try:
                    await page.locator('input[type="text"], input:not([type])').first.fill("Crystal Meet check")
                    await page.wait_for_timeout(500)
                    seen = await page.evaluate(DESCRIBE_JS)
                except Exception as e:
                    print(f"Typing a name failed: {e}", file=out)
            else:
                print("Name field: not found", file=out)
            if seen["joins"]:
                control = seen["joins"][0]
                state = "still disabled after typing a name" if control["disabled"] else "enabled after typing a name"
                print(f"Join control: found: {control['text']!r} ({control['tag']}"
                      f"{', role=button' if control['role'] == 'button' else ''}), {state}; "
                      f"the room's selectors match it: {'yes' if await _count(page, GoogleMeetProvider.JOIN_SELECTORS) else 'NO'}", file=out)
            else:
                print("Join control: not found", file=out)
            print(f'Meet says: "{seen["words"]}"', file=out)
            if screenshot is not None:
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot))
                print(f"Screenshot: {screenshot}", file=out)
            return 0 if seen["joins"] else 1
        finally:
            await context.close()
            if browser is not None:
                await browser.close()
