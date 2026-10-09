# TV screensaver and one page: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The TV Pi shows a selectable screensaver when idle and the meeting when joined, from one browser page the agent owns and navigates, with the style chosen on the room controller and remembered.

**Architecture:** A `TvDisplay` in the meeting service owns the only browser (kiosk window, the Meet sign-in profile when configured) and its one page, parked on a new `/tv` page served by the control service. Providers borrow that page: they navigate it to the meeting and ask the display to show the screensaver again when the meeting ends. The screensaver style lives in a settings file next to the agent's other state, is exposed on `/api/screensaver` and in `/api/status`, and the room page gets a picker.

**Tech Stack:** Python 3.12, Playwright (async), aiohttp control service, plain HTML/CSS/JS pages tested with Playwright's sync API, pytest.

**Spec:** `docs/superpowers/specs/2026-10-07-tv-screensaver-and-one-page-design.md`

## Global Constraints

- Internal names stay `croom`; what people see says Crystal Meet.
- The branch `tv-screensaver` is cut from `meet-account` and merges after it.
- Teams and Webex providers are not touched.
- Browser flags: `--use-fake-ui-for-media-stream`, `--autoplay-policy=no-user-gesture-required`, `--disable-infobars`, `--no-sandbox`, `--disable-setuid-sandbox`, `--disable-dev-shm-usage`, `--window-size=1920,1080`, plus `--kiosk` when `meeting.kiosk` is on. No `--disable-gpu`, no user-agent override.
- Styles are exactly `info`, `quiet`, `brand`, `bounce`; default `info`; brand palette for bounce `#1B52E5`, `#BDCEFF`, `#FFFFFF`, `#6C92F5`, `#1FA971`, `#E8A013`.
- POSTs stay JSON-only (415 otherwise), like the existing control API.
- Tests: `.venv/bin/pytest -q -p no:cacheprovider <path>`. Before the final commit: `bash /tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/suite-gate.sh tv-screensaver` and read `grep "GATE:"` (81 upstream failures is the baseline).
- Commit messages are plain, no attribution lines.

## Review Focus

1. The control service is not listening yet when the display first shows the screensaver: the display must keep retrying and land on the page once it is up, never raise (Task 1, `test_show_idle_keeps_trying_until_the_page_is_served`).
2. Chromium closes or crashes the page mid-day: the next `page()` must hand out a fresh page parked on the screensaver (Task 1, `test_a_closed_page_is_replaced_and_parked_on_idle`).
3. An unknown style posted to `/api/screensaver` must answer 400 and leave the stored style untouched (Task 3, `test_unknown_style_is_refused_and_nothing_changes`).
4. An unreadable or corrupt settings file must fall back to the configured default without crashing the control service (Task 3, `test_corrupt_settings_file_falls_back_to_the_default`).
5. Changing the style while the TV page is open must re-render without reloading the page, so the screensaver never flashes blank (Task 4, `test_style_change_re_renders_without_a_reload`).

---

### Task 1: `TvDisplay`

**Files:**
- Create: `src/croom/meeting/display.py`
- Modify: `src/croom/core/config.py` (`MeetingConfig.kiosk`, `to_dict`)
- Create: `tests/unit/meeting/test_display.py`

**Interfaces:**
- Produces: `TvDisplay(idle_url: str, profile_dir: Optional[str] = None, headless: bool = False, kiosk: bool = True)`, `TvDisplay.from_config(config) -> TvDisplay`, `BROWSER_ARGS`, `context_options()`, async `start()`, async `page() -> Page`, property `context`, property `profile_dir: Optional[Path]`, property `idle_url: str`, async `show_idle()`, async `stop()`; `MeetingConfig.kiosk: bool = True`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/meeting/test_display.py`:

```python
"""
The TV display owns the one browser page: parked on the screensaver, lent to a
provider, replaced if it dies (spec 2026-10-07 TV, section 4.3).
"""

import asyncio
import logging
from pathlib import Path

import pytest
from aiohttp import web

playwright = pytest.importorskip("playwright.async_api")

from croom.core.config import Config  # noqa: E402
from croom.meeting.display import TvDisplay  # noqa: E402


class IdleSite:
    """A stand-in for the control service's /tv page on a loopback port."""

    def __init__(self):
        self.runner = None
        self.port = None

    async def start(self, port=0):
        app = web.Application()
        app.router.add_get("/tv", lambda request: web.Response(text="<title>Crystal Meet TV</title><h1>Screensaver</h1>", content_type="text/html"))
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        self.port = self.runner.addresses[0][1]
        return self

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/tv"

    async def stop(self):
        await self.runner.cleanup()


async def test_start_parks_the_page_on_the_screensaver(tmp_path):
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    try:
        await display.start()
        page = await display.page()
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
        assert page.url == site.url
    finally:
        await display.stop()
        await site.stop()


async def test_a_closed_page_is_replaced_and_parked_on_idle(tmp_path):
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    try:
        await display.start()
        first = await display.page()
        await first.close()
        second = await display.page()
        assert second is not first and not second.is_closed()
        await second.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
    finally:
        await display.stop()
        await site.stop()


async def test_show_idle_keeps_trying_until_the_page_is_served(tmp_path, caplog):
    site = IdleSite()
    display = TvDisplay("http://127.0.0.1:1/tv", headless=True)   # nothing listens on port 1
    display.RETRY_EVERY_S = 0.2
    display.RETRY_FOR_S = 10
    try:
        with caplog.at_level(logging.WARNING):
            await display.start()                                 # must not raise
        await site.start()
        display._idle_url = site.url                              # the test moves the site under the display
        page = await display.page()
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=8000)
    finally:
        await display.stop()
        if site.runner:
            await site.stop()


async def test_profile_launch_makes_a_private_folder(tmp_path):
    site = await IdleSite().start()
    profile = tmp_path / "profile"
    display = TvDisplay(site.url, profile_dir=str(profile), headless=True)
    try:
        await display.start()
        assert oct(profile.stat().st_mode & 0o777) == "0o700"
        assert display.profile_dir == profile
        page = await display.page()
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
    finally:
        await display.stop()
        await site.stop()
    assert any(profile.iterdir())


async def test_unusable_profile_folder_is_a_clear_error(tmp_path):
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    display = TvDisplay("about:blank", profile_dir=str(parent / "profile"), headless=True)
    try:
        with pytest.raises(RuntimeError) as failure:
            await display.start()
        assert str(parent / "profile") in str(failure.value)
    finally:
        await display.stop()
        parent.chmod(0o700)


def test_from_config_reads_the_control_port_profile_and_kiosk():
    config = Config.from_dict({"control": {"enabled": True, "port": 8081},
                               "meeting": {"google_profile_dir": "/var/lib/croom/meet-profile", "kiosk": False}})
    display = TvDisplay.from_config(config)
    assert display.idle_url == "http://127.0.0.1:8081/tv"
    assert display.profile_dir == Path("/var/lib/croom/meet-profile")
    assert "--kiosk" not in display.browser_args()
    assert "--kiosk" in TvDisplay.from_config(Config()).browser_args()
    assert TvDisplay.from_config(Config.from_dict({"control": {"enabled": False}})).idle_url == "about:blank"
    assert Config().meeting.kiosk is True and Config.from_dict(Config().to_dict()).meeting.kiosk is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_display.py`
Expected: FAIL at import, `croom.meeting.display` does not exist.

- [ ] **Step 3: Add the config key**

In `src/croom/core/config.py` add to `MeetingConfig` after `google_profile_dir`:

```python
    kiosk: bool = True  # the TV browser fills the screen with no window chrome
```

and in `to_dict`'s `"meeting"` block after `google_profile_dir`:

```python
                "kiosk": self.meeting.kiosk,
```

- [ ] **Step 4: Write `display.py`**

Create `src/croom/meeting/display.py`:

```python
"""
The TV: one browser window, one page, owned here and lent to the meeting
providers (spec 2026-10-07 TV screensaver, section 4.3). Idle, the page sits on
the control service's /tv screensaver; a provider navigates it to the meeting
and hands it back through show_idle(). If the page dies, page() replaces it.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False


class TvDisplay:
    BASE_ARGS = [
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-infobars",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1920,1080",
    ]
    RETRY_EVERY_S = 2.0   # while the control service is still coming up
    RETRY_FOR_S = 30.0
    CLOSE_TIMEOUT_S = 5.0

    def __init__(self, idle_url: str, profile_dir: Optional[str] = None, headless: bool = False, kiosk: bool = True):
        self._idle_url = idle_url
        self._profile_dir: Optional[Path] = Path(profile_dir) if profile_dir else None
        self._headless = headless
        self._kiosk = kiosk
        self._playwright = None
        self._browser: Optional["Browser"] = None
        self._context: Optional["BrowserContext"] = None
        self._page: Optional["Page"] = None
        self._retry: Optional[asyncio.Task] = None

    @classmethod
    def from_config(cls, config) -> "TvDisplay":
        idle_url = f"http://127.0.0.1:{config.control.port}/tv" if config.control.enabled else "about:blank"
        return cls(idle_url, profile_dir=config.meeting.google_profile_dir or None, kiosk=config.meeting.kiosk)

    @classmethod
    def context_options(cls) -> dict:
        return {"permissions": ["camera", "microphone"], "viewport": {"width": 1920, "height": 1080}}

    def browser_args(self) -> list:
        return self.BASE_ARGS + (["--kiosk"] if self._kiosk else [])

    @property
    def idle_url(self) -> str:
        return self._idle_url

    @property
    def profile_dir(self) -> Optional[Path]:
        return self._profile_dir

    @property
    def context(self) -> Optional["BrowserContext"]:
        return self._context

    async def start(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")
        self._playwright = await async_playwright().start()
        if self._profile_dir is not None:
            try:
                self._profile_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(self._profile_dir, 0o700)
            except OSError as e:
                raise RuntimeError(f"Browser profile folder {self._profile_dir} is not usable: {e}") from e
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self._profile_dir), headless=self._headless, args=self.browser_args(), **self.context_options(),
            )
            logger.info(f"TV browser started on the signed-in profile at {self._profile_dir}")
        else:
            self._browser = await self._playwright.chromium.launch(headless=self._headless, args=self.browser_args())
            self._context = await self._browser.new_context(**self.context_options())
            logger.info("TV browser started as a guest")
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        await self.show_idle()

    async def page(self) -> "Page":
        """The shared page; a new one, parked on the screensaver, when the old one is gone."""
        if self._context is None:
            raise RuntimeError("TV display not started")
        if self._page is None or self._page.is_closed():
            logger.warning("The TV page was closed; opening a new one")
            self._page = await self._context.new_page()
            await self.show_idle()
        return self._page

    async def show_idle(self) -> None:
        """Park the page on the screensaver; keep trying for a while if the control service is not up yet."""
        if await self._try_idle():
            return
        if self._retry is None or self._retry.done():
            self._retry = asyncio.create_task(self._retry_idle())

    async def _try_idle(self) -> bool:
        if self._page is None or self._page.is_closed():
            return False
        try:
            await self._page.goto(self._idle_url, wait_until="commit")
            return True
        except Exception as e:  # noqa: BLE001 - the control service may not be listening yet
            logger.debug(f"Screensaver not reachable yet at {self._idle_url}: {e}")
            return False

    async def _retry_idle(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.RETRY_FOR_S
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self.RETRY_EVERY_S)
            if await self._try_idle():
                return
        logger.warning(f"The TV could not load the screensaver at {self._idle_url} for {self.RETRY_FOR_S:.0f} s")

    async def stop(self) -> None:
        if self._retry is not None and not self._retry.done():
            self._retry.cancel()
        self._retry = None
        for attribute in ("_context", "_browser"):
            closer = getattr(self, attribute)
            if closer is not None:
                try:
                    await asyncio.wait_for(closer.close(), timeout=self.CLOSE_TIMEOUT_S)
                except Exception as e:  # noqa: BLE001 - a hung browser must not hang the agent
                    logger.warning(f"TV browser {attribute[1:]} did not close cleanly: {e}")
                setattr(self, attribute, None)
        self._page = None
        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=self.CLOSE_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Playwright did not stop cleanly: {e}")
            self._playwright = None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_display.py tests/unit/core/test_config.py`
Expected: PASS. If `test_show_idle_keeps_trying_until_the_page_is_served` is flaky because the retry fires before the test moves the site, raise `RETRY_FOR_S` in the test; the retry loop reads `_idle_url` each attempt.

- [ ] **Step 6: Commit**

```bash
git add src/croom/meeting/display.py src/croom/core/config.py tests/unit/meeting/test_display.py
git commit -m "feat(tv): TvDisplay owns the one browser page and parks it on the screensaver"
```

---

### Task 2: Providers borrow the page; the meeting service owns the display

**Files:**
- Modify: `src/croom/meeting/providers/zoom_sdk.py` (constructor, `initialize`, `shutdown`, `_blank_page`, `join_meeting` start)
- Modify: `src/croom/meeting/providers/zoom.py` (constructor, `initialize`, `shutdown`, `leave_meeting`)
- Modify: `src/croom/meeting/providers/google_meet.py` (constructor, `from_config`, `initialize`, `shutdown`, `leave_meeting`, the `_profile_dir` use)
- Modify: `src/croom/meeting/providers/__init__.py` (`build_provider(platform, config, display=None)`)
- Modify: `src/croom/meeting/service.py` (`__init__`, `start`, `stop`)
- Modify: `tests/unit/meeting/test_zoom_sdk_provider.py`, `test_zoom_web_client.py`, `test_meet_prejoin.py`, `test_meet_profile.py`, `test_meet_selection.py`, `tests/unit/core/test_agent.py`

**Interfaces:**
- Consumes: `TvDisplay` from Task 1.
- Produces: `ZoomSdkProvider(credentials, room_name, api=None, display=None, extra_init_script=None, block_sdk_cdn=False, site=None)`, `ZoomProvider(display=None)`, `GoogleMeetProvider(display=None, room_name="Conference Room")`, each with `from_config(config, display)`; `build_provider(platform, config, display=None)`; `MeetingService(config, display=None)` whose `start()` builds providers, starts the display only when at least one provider exists, then initialises the providers; a `FakeDisplay` test helper in `tests/unit/meeting/fake_display.py` with `profile_dir`, `idle_url`, `page()`, `show_idle()`, `shown` counter and `context`.

- [ ] **Step 1: Write the test helper and update the tests**

Create `tests/unit/meeting/fake_display.py`:

```python
"""A display stand-in for provider tests that drive a page they create themselves."""

from pathlib import Path
from typing import Optional


class FakeDisplay:
    def __init__(self, page=None, profile_dir: Optional[str] = None, idle_url: str = "about:blank"):
        self._page = page
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self.idle_url = idle_url
        self.shown = 0
        self.context = getattr(page, "context", None)

    async def page(self):
        return self._page

    async def show_idle(self):
        self.shown += 1
        if self._page is not None and not self._page.is_closed():
            await self._page.goto(self.idle_url)
```

In `tests/unit/meeting/test_zoom_sdk_provider.py` replace `provider_for`:

```python
async def provider_for(credentials=FULL, api=None, stub=STUB_JS):
    from croom.meeting.display import TvDisplay
    display = TvDisplay("about:blank", headless=True)
    await display.start()
    provider = ZoomSdkProvider(credentials, room_name="Room 1", api=api, display=display,
                               extra_init_script=stub, block_sdk_cdn=True)
    await provider.initialize()
    provider._test_display = display
    return provider
```

and, because providers no longer close the browser, make every `await provider.shutdown()` in that file also stop the display: add after the imports

```python
async def shutdown(provider):
    await provider.shutdown()
    await provider._test_display.stop()
```

and replace each `await provider.shutdown()` in the tests with `await shutdown(provider)` (`sed -i 's/await provider.shutdown()/await shutdown(provider)/' tests/unit/meeting/test_zoom_sdk_provider.py`, then restore the one inside the new helper). Add one test to the file:

```python
class TestDisplay:
    async def test_leaving_parks_the_page_on_the_screensaver(self):
        provider = await provider_for()
        try:
            await provider.join_meeting(LINK)
            await provider.leave_meeting()
            assert provider._page.url == "about:blank"
            assert provider.state == MeetingState.IDLE
        finally:
            await shutdown(provider)
```

In `tests/unit/meeting/test_zoom_web_client.py` nothing changes: `ZoomProvider()` still works with no display and the tests set `_page` themselves.

In `tests/unit/meeting/test_meet_prejoin.py` replace every `GoogleMeetProvider(profile_dir=str(tmp_path / "profile"))` with `GoogleMeetProvider(FakeDisplay(profile_dir=str(tmp_path / "profile")))` and every bare `GoogleMeetProvider()` with `GoogleMeetProvider(FakeDisplay())`, adding `from tests.unit.meeting.fake_display import FakeDisplay  # noqa: E402` after the provider import.

Replace `tests/unit/meeting/test_meet_profile.py` entirely with:

```python
"""
The Meet provider on the shared TV page: it joins by navigating the display's
page and hands it back to the screensaver when it leaves.
"""

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.providers.google_meet import GoogleMeetProvider  # noqa: E402
from tests.unit.meeting.fake_display import FakeDisplay  # noqa: E402


async def test_leaving_hands_the_page_back_to_the_screensaver(tmp_path):
    idle = tmp_path / "idle.html"
    idle.write_text("<title>Crystal Meet TV</title>")
    meeting = tmp_path / "meeting.html"
    meeting.write_text("<title>Meet</title><button aria-label='Leave call'>Leave</button>")
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        display = FakeDisplay(page, idle_url=idle.as_uri())
        provider = GoogleMeetProvider(display, room_name="Room 3")
        await provider.initialize()
        assert provider._page is page
        await page.goto(meeting.as_uri())
        provider._set_state(provider.state.__class__.CONNECTED)
        await provider.leave_meeting()
        assert display.shown == 1 and page.url == idle.as_uri()
        await provider.shutdown()
        assert not page.is_closed()   # the display, not the provider, owns the page
        await browser.close()
```

In `tests/unit/meeting/test_meet_selection.py` replace the two provider tests with:

```python
def test_meet_provider_gets_the_display_and_the_room_name():
    from croom.meeting.display import TvDisplay
    config = Config.from_dict({"room": {"name": "Room 3"}, "meeting": {"google_profile_dir": "/tmp/meet-profile"}})
    display = TvDisplay.from_config(config)
    provider = build_provider("google_meet", config, display)
    assert isinstance(provider, GoogleMeetProvider)
    assert provider.display is display and display.profile_dir == Path("/tmp/meet-profile")
    assert provider.room_name == "Room 3"


def test_meet_provider_without_a_room_name_is_called_conference_room():
    provider = build_provider("google_meet", Config.from_dict({"room": {"name": ""}}), None)
    assert provider.room_name == "Conference Room"
```

In `tests/unit/core/test_agent.py` nothing changes: with every provider patched to `None`, the service never starts a display.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting/test_meet_selection.py tests/unit/meeting/test_meet_profile.py tests/unit/meeting/test_zoom_sdk_provider.py -x`
Expected: FAIL: `build_provider` takes no display, `GoogleMeetProvider` has no `display`, `ZoomSdkProvider` rejects `display=`.

- [ ] **Step 3: The Zoom SDK provider**

In `src/croom/meeting/providers/zoom_sdk.py`:

- Delete `BROWSER_ARGS` and `USER_AGENT` from the class.
- Constructor:

```python
    def __init__(self, credentials: ZoomCredentials, room_name: str = "Conference Room",
                 api: Optional[ZoomApi] = None, display=None,
                 extra_init_script: Optional[str] = None, block_sdk_cdn: bool = False,
                 site: Optional[ZoomSdkSite] = None):
        super().__init__()
        self._credentials = credentials
        self._room_name = room_name
        if api is None and credentials.has_room_user:
            api = ZoomApi(credentials.account_id, credentials.s2s_client_id, credentials.s2s_client_secret)
        self._api = api
        self._display = display
        self._extra_init_script = extra_init_script
        self._block_sdk_cdn = block_sdk_cdn
        self._site = site or ZoomSdkSite()
        self._page = None
        self._exposed_on = None   # the page object crystalMeetEvent was exposed on
        self._events: Optional[asyncio.Queue] = None
        self._join_task: Optional[asyncio.Task] = None
        self._watcher: Optional[asyncio.Task] = None
        self._muted = False
        self._camera_on = True
```

- `from_config(cls, config, display=None)` passes `display=display`.
- `initialize`:

```python
    async def initialize(self) -> None:
        if self._display is None:
            raise RuntimeError("Zoom SDK provider needs the TV display")
        self._events = asyncio.Queue()
        await self._site.start()
        context = self._display.context
        if context is not None and self._block_sdk_cdn:
            async def abort(route):
                await route.abort()
            await context.route("https://source.zoom.us/**", abort)
        if context is not None and self._extra_init_script:
            await context.add_init_script(self._extra_init_script)
        await self._take_page()
        logger.info(f"Zoom Meeting SDK provider ready (page on http://127.0.0.1:{self._site.port}/meeting)")

    async def _take_page(self) -> None:
        """Borrow the display's page and make sure the bridge can reach us from it."""
        self._page = await self._display.page()
        if self._exposed_on is not self._page:
            await self._page.expose_function("crystalMeetEvent", self._on_page_event)
            self._exposed_on = self._page
```

- `shutdown`:

```python
    async def shutdown(self) -> None:
        await self._cancel_tasks()
        if self._state == MeetingState.CONNECTED:
            await self.leave_meeting()
        self._page = None
        await self._site.stop()
```

- `_blank_page` becomes the hand-back:

```python
    async def _blank_page(self) -> None:
        """Hand the page back to the screensaver; the page may already be gone."""
        try:
            await self._display.show_idle()
        except Exception:  # noqa: BLE001
            pass
```

- In `join_meeting`, replace the opening guard `if self._page is None: raise RuntimeError("Provider not initialized")` with:

```python
        if self._display is None:
            raise RuntimeError("Provider not initialized")
        await self._take_page()
```

- [ ] **Step 4: The Zoom web-client provider**

In `src/croom/meeting/providers/zoom.py`:

- Constructor `def __init__(self, display=None)` keeping `self._display = display` and `self._page = None`; drop `_playwright`, `_browser`, `_context`.
- Add `from_config(cls, config, display=None)` returning `cls(display=display)`.
- `initialize`:

```python
    async def initialize(self) -> None:
        """Borrow the TV page."""
        if self._display is None:
            raise RuntimeError("Zoom provider needs the TV display")
        self._page = await self._display.page()
        logger.info("Zoom provider initialized on the TV page")
```

- `shutdown`: leave the meeting if connected, then `self._page = None`; nothing else.
- In `join_meeting`, after the URL checks and before the first `goto`, add `if self._display is not None: self._page = await self._display.page()`.
- In `leave_meeting`, replace `await self._page.goto("about:blank")` with `await self._show_idle()` and add:

```python
    async def _show_idle(self) -> None:
        if self._display is not None:
            await self._display.show_idle()
        elif self._page is not None:
            await self._page.goto("about:blank")
```

- [ ] **Step 5: The Meet provider**

In `src/croom/meeting/providers/google_meet.py`:

- Delete `BROWSER_ARGS`, `context_options`, the `os`/`tempfile`-free parts stay; constructor:

```python
    def __init__(self, display=None, room_name: str = "Conference Room"):
        super().__init__()
        self._display = display
        self._room_name = room_name or "Conference Room"
        # Where a screenshot goes when a join fails, so the TV need not be watched.
        self.failure_screenshot: Path = Path(tempfile.gettempdir()) / "croom-meet-failure.png"
        self._page: Optional["Page"] = None

    @classmethod
    def from_config(cls, config, display=None) -> "GoogleMeetProvider":
        return cls(display, room_name=config.room.name or "Conference Room")

    @property
    def display(self):
        return self._display

    @property
    def room_name(self) -> str:
        return self._room_name
```

  (remove the `profile_dir` property and `_profile_dir`.)
- `initialize`:

```python
    async def initialize(self) -> None:
        """Borrow the TV page; the display owns the browser and the signed-in profile."""
        if self._display is None:
            raise RuntimeError("Google Meet provider needs the TV display")
        self._page = await self._display.page()
        logger.info("Google Meet provider ready on the TV page")
```

- `shutdown`: leave if connected, then `self._page = None`.
- In `join_meeting`, replace `if not self._page: raise RuntimeError("Provider not initialized")` with `if self._display is None and not self._page: raise RuntimeError("Provider not initialized")` followed by `if self._display is not None: self._page = await self._display.page()`.
- In `_handle_prejoin`, the branch `elif self._profile_dir is None:` becomes `elif self._display is None or self._display.profile_dir is None:`.
- In `leave_meeting`, replace `await self._page.goto("about:blank")` with `await self._show_idle()` and add the same `_show_idle` helper as in `zoom.py`.
- Remove the now-unused imports (`os`, `async_playwright` if unused; keep `PLAYWRIGHT_AVAILABLE` checks only where still referenced). Run `python -m pyflakes` if available, otherwise rely on tests.

- [ ] **Step 6: The factory and the service**

In `src/croom/meeting/providers/__init__.py`:

```python
def build_provider(platform: str, config, display=None) -> "MeetingProvider | None":
    ...
        if reason is None:
            return ZoomSdkProvider.from_config(config, display)
        logger.warning(...)
        return ZoomProvider(display=display)
    if platform == "google_meet":
        from croom.meeting.providers.google_meet import GoogleMeetProvider

        return GoogleMeetProvider.from_config(config, display)
    provider_cls = get_provider(platform)
    return provider_cls() if provider_cls else None
```

In `src/croom/meeting/service.py` add `from croom.meeting.display import TvDisplay` and change:

```python
    def __init__(self, config: Config, display: Optional[TvDisplay] = None):
        super().__init__("meeting")
        self.config = config
        self._display = display
        self._providers: Dict[str, MeetingProvider] = {}
        ...

    async def start(self) -> None:
        """Start the TV display and the configured providers on it."""
        if self._display is None:
            self._display = TvDisplay.from_config(self.config)
        built = {}
        for platform in self.config.meeting.platforms:
            try:
                provider = build_provider(platform, self.config, self._display)
            except Exception as e:  # noqa: BLE001 - a bad credentials file must not stop the other platforms
                logger.error(f"Failed to build {platform} provider: {e}")
                continue
            if provider is not None:
                built[platform] = provider
        if not built:
            logger.warning("No meeting providers available")
            return
        try:
            await self._display.start()
        except Exception as e:  # noqa: BLE001
            logger.error(f"The TV display could not start: {e}")
            return
        for platform, provider in built.items():
            try:
                await provider.initialize()
                self._providers[platform] = provider
                logger.info(f"Initialized meeting provider: {platform}")
            except Exception as e:
                logger.error(f"Failed to initialize {platform} provider: {e}")
        logger.info(f"Meeting service started with {len(self._providers)} providers")

    async def stop(self) -> None:
        ... (existing provider shutdown loop) ...
        self._providers.clear()
        self._active_provider = None
        if self._display is not None:
            await self._display.stop()
        logger.info("Meeting service stopped")
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/meeting tests/unit/core/test_agent.py tests/unit/control/test_service.py`
Expected: PASS (the Zoom SDK stub tests take a few minutes).

- [ ] **Step 8: Commit**

```bash
git add src/croom/meeting/providers/zoom_sdk.py src/croom/meeting/providers/zoom.py src/croom/meeting/providers/google_meet.py src/croom/meeting/providers/__init__.py src/croom/meeting/service.py tests/unit/meeting/fake_display.py tests/unit/meeting/test_zoom_sdk_provider.py tests/unit/meeting/test_meet_prejoin.py tests/unit/meeting/test_meet_profile.py tests/unit/meeting/test_meet_selection.py
git commit -m "feat(tv): providers borrow the display's page and hand it back to the screensaver"
```

---

### Task 3: The screensaver setting and its API

**Files:**
- Modify: `src/croom/core/config.py` (`ControlConfig.screensaver`, `to_dict`)
- Modify: `src/croom/control/service.py` (settings, routes, status)
- Modify: `tests/unit/control/test_service.py`

**Interfaces:**
- Produces: `STYLES = ("info", "quiet", "brand", "bounce")`; config keys `settings_file` and `screensaver` on `ControlService`; `GET/POST /api/screensaver`; `screensaver` in `/api/status`; `ControlService.screensaver` property.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/control/test_service.py` (it already has `StubMeeting`, `StubCalendar`, `TestClient`, `TestServer`; reuse its `client` fixture pattern, or add the small helper below if the file has none):

```python


async def screensaver_client(tmp_path, **overrides):
    config = {"host": "127.0.0.1", "port": 0, "room_name": "Lab", "settings_file": str(tmp_path / "control-settings.json")}
    config.update(overrides)
    service = ControlService(config=config, meeting=StubMeeting(), calendar=StubCalendar(events=[]))
    client = TestClient(TestServer(service.create_app()))
    await client.start_server()
    return service, client


async def test_screensaver_defaults_to_info_and_appears_in_status(tmp_path):
    service, client = await screensaver_client(tmp_path)
    try:
        data = await (await client.get("/api/screensaver")).json()
        assert data == {"style": "info", "styles": ["info", "quiet", "brand", "bounce"]}
        assert (await (await client.get("/api/status")).json())["screensaver"] == "info"
    finally:
        await client.close()


async def test_a_posted_style_is_stored_and_survives_a_restart(tmp_path):
    service, client = await screensaver_client(tmp_path)
    try:
        response = await client.post("/api/screensaver", json={"style": "bounce"})
        assert response.status == 200 and (await response.json())["style"] == "bounce"
        assert (await (await client.get("/api/status")).json())["screensaver"] == "bounce"
        settings = tmp_path / "control-settings.json"
        assert oct(settings.stat().st_mode & 0o777) == "0o600"
    finally:
        await client.close()
    again, client = await screensaver_client(tmp_path)
    try:
        assert (await (await client.get("/api/screensaver")).json())["style"] == "bounce"
    finally:
        await client.close()


async def test_unknown_style_is_refused_and_nothing_changes(tmp_path):
    service, client = await screensaver_client(tmp_path)
    try:
        await client.post("/api/screensaver", json={"style": "quiet"})
        response = await client.post("/api/screensaver", json={"style": "disco"})
        assert response.status == 400 and "disco" in (await response.json())["error"]
        assert (await (await client.get("/api/screensaver")).json())["style"] == "quiet"
        assert (await client.post("/api/screensaver", data="style=brand")).status == 415
    finally:
        await client.close()


async def test_corrupt_settings_file_falls_back_to_the_default(tmp_path):
    (tmp_path / "control-settings.json").write_text("{not json")
    service, client = await screensaver_client(tmp_path, screensaver="brand")
    try:
        assert (await (await client.get("/api/screensaver")).json())["style"] == "brand"
    finally:
        await client.close()


def test_control_config_has_a_default_style():
    assert Config().control.screensaver == "info"
    config = Config.from_dict({"control": {"screensaver": "quiet"}})
    assert Config.from_dict(config.to_dict()).control.screensaver == "quiet"
    service = ControlService.from_config(config)
    assert service.config["screensaver"] == "quiet"
    assert service.config["settings_file"].endswith("control-settings.json")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_service.py -k "screensaver or style or settings"`
Expected: FAIL: 404 on `/api/screensaver`, no `screensaver` in status, `ControlConfig` has no `screensaver`.

- [ ] **Step 3: Implement**

In `src/croom/core/config.py` add to `ControlConfig`:

```python
    screensaver: str = "info"  # what the TV shows when idle until someone picks another style on the room page
```

and `"screensaver": self.control.screensaver,` to `to_dict`'s `"control"` block.

In `src/croom/control/service.py`:

- add `import json`, `import os`, `import tempfile` if missing, and the constant `STYLES = ("info", "quiet", "brand", "bounce")` next to `EVENT_FIELDS`.
- in `__init__`: `self._settings_file = Path(self.config.get("settings_file", "control-settings.json"))`, `self._default_style = str(self.config.get("screensaver", "info"))`, `self._screensaver = self._load_style()`.
- in `from_config`'s config dict: `"screensaver": config.control.screensaver, "settings_file": str(config.resolve_data_dir() / "control-settings.json"),`.
- routes: `app.router.add_get("/api/screensaver", self._handle_screensaver)` and `app.router.add_post("/api/screensaver", self._handle_set_screensaver)`.
- in `_status()`'s returned dict add `"screensaver": self._screensaver,`.
- methods:

```python
    @property
    def screensaver(self) -> str:
        return self._screensaver

    def _load_style(self) -> str:
        try:
            data = json.loads(self._settings_file.read_text(encoding="utf-8"))
            style = data.get("screensaver") if isinstance(data, dict) else None
        except (OSError, ValueError):
            style = None
        if style in STYLES:
            return style
        return self._default_style if self._default_style in STYLES else "info"

    def _save_style(self, style: str) -> None:
        self._settings_file.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=str(self._settings_file.parent), prefix=".control-settings-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"screensaver": style}, handle)
            os.chmod(temp, 0o600)
            os.replace(temp, self._settings_file)
        except OSError:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def _screensaver_payload(self) -> Dict[str, Any]:
        return {"style": self._screensaver, "styles": list(STYLES)}

    async def _handle_screensaver(self, request: web.Request) -> web.Response:
        return web.json_response(self._screensaver_payload())

    async def _handle_set_screensaver(self, request: web.Request) -> web.Response:
        refused = self._require_json(request)
        if refused is not None:
            return refused
        data = await self._read_object(request)
        style = data.get("style") if data else None
        if style not in STYLES:
            return web.json_response({"error": f"Unknown screensaver style {style!r}; use one of {', '.join(STYLES)}"}, status=400)
        try:
            self._save_style(style)
        except OSError as e:
            logger.warning(f"Could not save the screensaver choice to {self._settings_file}: {e}")
        self._screensaver = style
        logger.info(f"Screensaver style set to {style}")
        return web.json_response(self._screensaver_payload())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_service.py tests/unit/core/test_config.py tests/unit/deploy/test_room_configs.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/croom/core/config.py src/croom/control/service.py tests/unit/control/test_service.py
git commit -m "feat(control): the screensaver style, stored next to the agent's state and served on /api/screensaver"
```

---

### Task 4: The TV page

**Files:**
- Create: `src/croom/control/static/tv.html`, `tv.css`, `tv.js`
- Modify: `src/croom/control/service.py` (`/tv` route)
- Create: `tests/unit/control/test_tv_page.py`

**Interfaces:**
- Consumes: `/api/status` with `screensaver`, `/api/calendar/events`, `/api/screensaver` from Task 3; `PageServer` from `tests/unit/control/test_page.py`.
- Produces: `GET /tv`; the page's `data-style` attribute on `<body>` and `data-state` like the sign; `window.__tvLoadedAt` set once at load (the no-reload proof).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/control/test_tv_page.py`:

```python
"""
The TV screensaver page in the venv's Chromium: four styles from the status
and bookings, switched in place when the room page picks another one.
"""

import pytest

from tests.unit.control.test_page import PageServer, browser, event  # noqa: F401 - fixture and helpers

playwright = pytest.importorskip("playwright.sync_api")


def open_tv(browser, server, width=1280, height=720):
    page = browser.new_page(viewport={"width": width, "height": height})
    page.goto(f"http://127.0.0.1:{server.port}/tv", wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    return page


def set_style(page, server, style):
    page.request.post(f"http://127.0.0.1:{server.port}/api/screensaver", data={"style": style},
                      headers={"Content-Type": "application/json"})
    page.wait_for_function(f"document.body.dataset.style === '{style}'", timeout=5000)


def test_info_style_shows_name_status_next_booking_and_clock(browser):
    with PageServer(calendar_events=[event("e1", "Design review", 25)], room_name="Room 3") as server:
        page = open_tv(browser, server)
        assert page.locator("body").get_attribute("data-style") == "info"
        assert page.locator("#room-name").inner_text() == "Room 3"
        assert page.locator("#headline").inner_text().startswith("Free until")
        assert "Design review" in page.locator("#detail").inner_text()
        assert page.locator("#clock").inner_text() != ""
        assert "Press Join on the controller" in page.locator("#hint").inner_text()
        page.close()


def test_quiet_style_shows_only_name_and_status(browser):
    with PageServer(room_name="Room 3") as server:
        page = open_tv(browser, server)
        set_style(page, server, "quiet")
        assert page.locator("#room-name").is_visible() and page.locator("#headline").is_visible()
        assert page.locator("#detail").is_hidden() and page.locator("#clock").is_hidden() and page.locator("#hint").is_hidden()
        page.close()


def test_brand_style_shows_the_logo_and_nothing_live(browser):
    with PageServer() as server:
        page = open_tv(browser, server)
        set_style(page, server, "brand")
        assert page.locator("#brand-logo").is_visible()
        assert page.locator("#headline").is_hidden() and page.locator("#room-name").is_hidden()
        page.close()


def test_bounce_moves_the_logo_and_changes_colour_at_an_edge(browser):
    with PageServer() as server:
        page = open_tv(browser, server, width=600, height=400)   # small, so an edge comes quickly
        set_style(page, server, "bounce")
        logo = page.locator("#bounce-logo")
        first = logo.bounding_box()
        colour_before = page.evaluate("getComputedStyle(document.getElementById('bounce-logo')).color")
        page.wait_for_timeout(600)
        second = logo.bounding_box()
        assert (first["x"], first["y"]) != (second["x"], second["y"])
        page.wait_for_function(
            f"getComputedStyle(document.getElementById('bounce-logo')).color !== '{colour_before}'", timeout=8000)
        page.close()


def test_style_change_re_renders_without_a_reload(browser):
    with PageServer() as server:
        page = open_tv(browser, server)
        loaded_at = page.evaluate("window.__tvLoadedAt")
        set_style(page, server, "bounce")
        set_style(page, server, "info")
        assert page.evaluate("window.__tvLoadedAt") == loaded_at
        page.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_tv_page.py`
Expected: FAIL, `/tv` answers 404 so the first wait times out.

- [ ] **Step 3: Serve the page**

In `src/croom/control/service.py` add the route `app.router.add_get("/tv", self._handle_tv)` and:

```python
    async def _handle_tv(self, request: web.Request) -> web.StreamResponse:
        tv = self._static_dir / "tv.html"
        if not tv.is_file():
            return web.Response(text="TV page assets are missing.", status=500)
        return web.FileResponse(tv, headers={"Cache-Control": "no-cache"})
```

Create `src/croom/control/static/tv.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crystal Meet</title>
<link rel="icon" href="/static/crystalpm-logo-white.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/tv.css">
</head>
<body data-state="loading" data-style="info">
<main class="tv">
  <header class="top">
    <img id="top-logo" class="logo" src="/static/crystalpm-logo-white.svg" alt="Crystal PM">
    <p id="clock" class="clock"></p>
  </header>
  <section class="status" aria-live="polite">
    <p id="room-name" class="room"></p>
    <p id="kicker" class="kicker">Connecting</p>
    <p id="headline" class="headline">Connecting</p>
    <p id="detail" class="detail"></p>
  </section>
  <p id="hint" class="hint">Press Join on the controller</p>
  <img id="brand-logo" class="brand-logo" src="/static/crystalpm-logo-white.svg" alt="Crystal PM" hidden>
  <div id="bounce-logo" class="bounce-logo" hidden>
    <svg viewBox="0 0 2000 541.43" aria-hidden="true"><use href="/static/crystalpm-logo-white.svg#Layer_1"></use></svg>
  </div>
</main>
<script src="/static/tv.js"></script>
</body>
</html>
```

The bouncing logo must take its colour from CSS, and the white SVG hard-codes `#ffffff`. Do not rely on `<use>` recolouring: instead, in `tv.js`, fetch `/static/crystalpm-logo-white.svg` once, replace every `#ffffff`/`#fff` fill with `currentColor`, and inject the result into `#bounce-logo` (replace the `<svg>` placeholder). Keep the placeholder so the layout exists before the fetch completes.

Create `src/croom/control/static/tv.css`:

```css
/* Crystal Meet TV: what the room's TV shows between meetings. */
@font-face { font-family: "Lexend"; src: url("/static/fonts/lexend-400.woff2") format("woff2"); font-weight: 400; font-display: swap; }
@font-face { font-family: "Lexend"; src: url("/static/fonts/lexend-600.woff2") format("woff2"); font-weight: 600; font-display: swap; }

:root {
  --navy-900: #001636; --navy-800: #16244F; --surface-900: #111827; --periwinkle-300: #BDCEFF;
  --free: #1FA971; --soon: #E8A013; --busy: #B42318; --gutter: clamp(32px, 4vw, 72px);
  --font: "Lexend", "Segoe UI", Arial, sans-serif;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0; cursor: none; overflow: hidden;
  background: linear-gradient(135deg, var(--navy-900) 0%, var(--navy-800) 100%);
  color: #fff; font-family: var(--font); font-size: clamp(18px, 1.6vw, 28px); line-height: 1.4;
}
.tv { min-height: 100%; padding: var(--gutter); display: grid; grid-template-rows: auto 1fr auto; gap: 24px; }
.top { display: flex; justify-content: space-between; align-items: flex-start; }
.logo { width: clamp(120px, 10vw, 200px); height: auto; }
.clock { margin: 0; font-size: clamp(2.5rem, 6vw, 6rem); font-weight: 400; line-height: 1; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.status { align-self: center; }
.room { margin: 0 0 8px; font-size: clamp(1.4rem, 2.4vw, 2.6rem); font-weight: 600; opacity: 0.92; }
.kicker { margin: 0 0 10px; font-size: clamp(0.8rem, 1.2vw, 1.2rem); font-weight: 700; letter-spacing: 0.15em; text-transform: uppercase; }
.headline { margin: 0; font-size: clamp(2.6rem, 7.5vw, 8rem); font-weight: 600; line-height: 1.02; letter-spacing: -0.02em; max-width: 16ch; }
.detail { margin: 16px 0 0; font-size: clamp(1.2rem, 2.2vw, 2.2rem); max-width: 44ch; opacity: 0.92; }
.detail:empty { display: none; }
.hint { margin: 0; font-size: clamp(1rem, 1.6vw, 1.6rem); color: var(--periwinkle-300); }

/* status colour as an accent, not the whole screen: the TV is big and dark */
body[data-state="free"] .kicker { color: var(--free); }
body[data-state="soon"] .kicker { color: var(--soon); }
body[data-state="occupied"] .kicker { color: #FF8A80; }
body[data-state="offline"] .kicker, body[data-state="loading"] .kicker { color: var(--periwinkle-300); }

/* styles */
body[data-style="quiet"] .clock, body[data-style="quiet"] .detail, body[data-style="quiet"] .hint { display: none; }
body[data-style="brand"] .top, body[data-style="brand"] .status, body[data-style="brand"] .hint { display: none; }
body[data-style="brand"] .brand-logo { display: block; }
.brand-logo { position: fixed; left: 50%; top: 50%; width: min(48vw, 900px); transform: translate(-50%, -50%); }
body[data-style="bounce"] .top, body[data-style="bounce"] .status, body[data-style="bounce"] .hint { display: none; }
body[data-style="bounce"] { background: #000; }
body[data-style="bounce"] .bounce-logo { display: block; }
.bounce-logo { position: fixed; left: 0; top: 0; width: 320px; color: #1B52E5; will-change: transform; }
.bounce-logo svg { width: 100%; height: auto; display: block; }
```

Create `src/croom/control/static/tv.js`:

```js
(function () {
  "use strict";

  window.__tvLoadedAt = Date.now();

  const SOON_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 2000;
  const EVENTS_EVERY_MS = 60000;
  const IN_PROGRESS = ["joining", "in_lobby", "connected", "leaving"];
  const STYLES = ["info", "quiet", "brand", "bounce"];
  const PALETTE = ["#1B52E5", "#BDCEFF", "#FFFFFF", "#6C92F5", "#1FA971", "#E8A013"];
  const SPEED = 140; // pixels per second

  const el = (id) => document.getElementById(id);
  const model = { status: null, events: [], offline: false, style: "info" };
  const fmtTime = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");

  async function getJson(path) {
    const options = typeof AbortSignal !== "undefined" && AbortSignal.timeout ? { signal: AbortSignal.timeout(4000) } : {};
    const response = await fetch(path, options);
    if (!response.ok) throw new Error("Request failed (" + response.status + ")");
    return response.json();
  }

  async function refreshStatus() {
    try {
      model.status = await getJson("/api/status");
      model.offline = false;
      if (STYLES.includes(model.status.screensaver)) model.style = model.status.screensaver;
    } catch (e) {
      model.offline = true;
    }
    render();
  }

  async function refreshEvents() {
    try {
      model.events = (await getJson("/api/calendar/events")).events || [];
    } catch (e) {
      // keep the last list
    }
    render();
  }

  function setStatus(state, kicker, headline, detail) {
    document.body.dataset.state = state;
    el("kicker").textContent = kicker;
    el("headline").textContent = headline;
    el("detail").textContent = detail || "";
  }

  const startMs = (ev) => new Date(ev.start_time).getTime();
  const endMs = (ev) => new Date(ev.end_time).getTime();
  const ongoingEvent = (now) => model.events.filter((ev) => startMs(ev) <= now && endMs(ev) > now).sort((a, b) => endMs(a) - endMs(b))[0] || null;
  const upcomingEvent = (now) => model.events.filter((ev) => startMs(ev) > now).sort((a, b) => startMs(a) - startMs(b))[0] || null;
  const earliest = (a, b) => (!a ? b : !b ? a : startMs(a) <= startMs(b) ? a : b);

  function render() {
    document.body.dataset.style = model.style;
    el("brand-logo").hidden = model.style !== "brand";
    el("bounce-logo").hidden = model.style !== "bounce";
    bounce.setActive(model.style === "bounce");
    if (!model.status && !model.offline) return;
    if (model.offline) {
      setStatus("offline", "Not connected", "Not connected", "The room's service is not answering.");
      return;
    }
    const s = model.status;
    el("room-name").textContent = s.room.name;
    const m = s.meeting;
    const cal = s.calendar;
    const now = Date.now();
    const current = cal.current || ongoingEvent(now);
    const next = earliest(cal.next, upcomingEvent(now));
    if (IN_PROGRESS.includes(m.state)) {
      setStatus("occupied", "In use", "In use", m.title || "");
    } else if (current) {
      setStatus("occupied", "Booked", "Booked until " + fmtTime(new Date(current.end_time)), current.title);
    } else if (next && startMs(next) - now <= SOON_MS) {
      const minutes = Math.max(0, Math.round((startMs(next) - now) / 60000));
      setStatus("soon", "Starting soon", minutes === 0 ? next.title + " is starting" : next.title + " starts in " + plural(minutes, "minute"), fmtTime(new Date(next.start_time)) + " to " + fmtTime(new Date(next.end_time)));
    } else if (next) {
      setStatus("free", "Available", "Free until " + fmtTime(new Date(next.start_time)), "Next: " + next.title);
    } else {
      setStatus("free", "Available", cal.connected ? "Free for the rest of the day" : "Free", cal.connected ? "Nothing else is booked in here today." : "");
    }
  }

  // The DVD-style bounce: constant speed, a new brand colour at every edge.
  const bounce = (function () {
    const box = el("bounce-logo");
    let active = false, x = 40, y = 40, dx = 1, dy = 1, colour = 0, last = null, frame = null;
    function paint() {
      box.style.transform = "translate(" + Math.round(x) + "px, " + Math.round(y) + "px)";
      box.style.color = PALETTE[colour];
    }
    function step(ts) {
      if (!active) return;
      const dt = last === null ? 0 : Math.min(0.05, (ts - last) / 1000);
      last = ts;
      const w = box.offsetWidth, h = box.offsetHeight;
      const maxX = Math.max(0, window.innerWidth - w), maxY = Math.max(0, window.innerHeight - h);
      x += dx * SPEED * dt;
      y += dy * SPEED * dt;
      let hit = false;
      if (x <= 0) { x = 0; dx = 1; hit = true; } else if (x >= maxX) { x = maxX; dx = -1; hit = true; }
      if (y <= 0) { y = 0; dy = 1; hit = true; } else if (y >= maxY) { y = maxY; dy = -1; hit = true; }
      if (hit) colour = (colour + 1) % PALETTE.length;
      paint();
      frame = requestAnimationFrame(step);
    }
    return {
      setActive(on) {
        if (on === active) return;
        active = on;
        last = null;
        if (frame) cancelAnimationFrame(frame);
        frame = active ? requestAnimationFrame(step) : null;
        if (active) paint();
      },
    };
  })();

  async function inlineLogo() {
    try {
      const svg = await (await fetch("/static/crystalpm-logo-white.svg")).text();
      el("bounce-logo").innerHTML = svg.replace(/#ffffff/gi, "currentColor").replace(/#fff\b/gi, "currentColor");
    } catch (e) {
      // the placeholder stays
    }
  }

  function tick() {
    el("clock").textContent = fmtTime(new Date());
  }

  inlineLogo();
  tick();
  setInterval(tick, 10000);
  refreshStatus();
  refreshEvents();
  setInterval(refreshStatus, STATUS_EVERY_MS);
  setInterval(refreshEvents, EVENTS_EVERY_MS);
})();
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_tv_page.py tests/unit/control/test_page.py`
Expected: PASS. If the bounce test's colour never changes within 8 s, the viewport is too large for the speed; the test uses 600 by 400 so an edge is hit within about 3 s.

- [ ] **Step 5: Look at it**

Run a quick render: start a `PageServer` in a script, open `/tv` at 1920 by 1080 with Playwright and screenshot each style to the scratchpad; view them. Adjust sizes in `tv.css` if anything overflows.

- [ ] **Step 6: Commit**

```bash
git add src/croom/control/static/tv.html src/croom/control/static/tv.css src/croom/control/static/tv.js src/croom/control/service.py tests/unit/control/test_tv_page.py
git commit -m "feat(control): the TV page with four screensaver styles"
```

---

### Task 5: The picker on the room page

**Files:**
- Modify: `src/croom/control/static/index.html`, `app.js`, `style.css`
- Modify: `tests/unit/control/test_page.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/control/test_page.py`:

```python


def test_the_screen_picker_posts_the_style_and_marks_the_current_one(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        picker = page.locator("#screen-picker")
        assert picker.is_visible()
        assert picker.locator("button[aria-pressed='true']").inner_text() == "Information"
        page.click("#screen-picker button:has-text('Bounce')")
        page.wait_for_function("document.querySelector(\"#screen-picker button[aria-pressed='true']\").innerText === 'Bounce'", timeout=5000)
        assert page.request.get(f"http://127.0.0.1:{server.port}/api/screensaver").json()["style"] == "bounce"
        page.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py -k picker`
Expected: FAIL, no `#screen-picker`.

- [ ] **Step 3: Implement**

In `src/croom/control/static/index.html` add after the `.link` section, before `</main>`:

```html

  <section class="screen-section">
    <p class="kicker">TV when idle</p>
    <div id="screen-picker" class="picker" role="group" aria-label="Screensaver style"></div>
  </section>
```

In `src/croom/control/static/app.js`:

- add to the constants: `const STYLE_LABELS = { info: "Information", quiet: "Quiet", brand: "Brand", bounce: "Bounce" };` and to `model`: `screensaver: null`.
- in `refreshStatus`, after the status is stored: `if (model.status.screensaver) model.screensaver = model.status.screensaver;`.
- add:

```js
  const setScreensaver = (style) => act(() => post("/api/screensaver", { style: style }).then((data) => { model.screensaver = data.style; renderPicker(); }));

  function renderPicker() {
    const picker = el("screen-picker");
    const buttons = Object.keys(STYLE_LABELS).map((style) => {
      const b = button(STYLE_LABELS[style], model.screensaver === style ? "primary" : "quiet", () => setScreensaver(style), false);
      b.setAttribute("aria-pressed", model.screensaver === style ? "true" : "false");
      return b;
    });
    picker.replaceChildren(...buttons);
  }
```

- in `render()`, after `document.querySelector(".link").hidden = ...`, add `document.querySelector(".screen-section").hidden = false;` and at the end of the offline branch `document.querySelector(".screen-section").hidden = true;`; call `renderPicker()` at the end of `render()` (after `renderEvents(cal)`). Check how `post` is defined near the `joinLink` helpers and reuse it exactly.

In `src/croom/control/static/style.css` add:

```css
.screen-section { grid-area: screen; }
.picker { display: flex; flex-wrap: wrap; gap: 10px; }
.picker .button { min-height: 44px; padding: 0 16px; }
```

and add `"screen"` to the page's grid-template-areas wherever `"link"` is laid out (both the wide and the narrow layouts), placing it under the link section.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py tests/unit/control/test_tv_page.py`
Expected: PASS, including `test_long_names_do_not_overflow_on_phone`.

- [ ] **Step 5: Commit**

```bash
git add src/croom/control/static/index.html src/croom/control/static/app.js src/croom/control/static/style.css tests/unit/control/test_page.py
git commit -m "feat(control): choose the TV screensaver from the room page"
```

---

### Task 6: README and the room setup guide

**Files:**
- Modify: `README.md`, `docs/guides/crystal-meet-room-setup/index.html` and its PDF
- Modify: `tests/unit/docs/test_readme.py`, `tests/unit/docs/test_room_setup_guide.py`

- [ ] **Step 1: Update the tests**

In `tests/unit/docs/test_readme.py` add the needles `"screensaver"`, `":8080/tv"` and `"docs/superpowers/specs/2026-10-07-tv-screensaver-and-one-page-design.md"` is covered by the links test once the table row exists. In `tests/unit/docs/test_room_setup_guide.py` append:

```python


def test_guide_describes_the_tv_screensaver():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "screensaver" in html and "TV when idle" in html
```

Run both files: FAIL on the new needles.

- [ ] **Step 2: Edit the README**

- Pieces table, room device row: append to its description " Between meetings the TV shows a screensaver chosen on the room page; one browser page is on the TV at a time."
- Add a row to the pieces table after the door sign: `| TV page \`http://<device>:8080/tv\` | The room's TV, in the device's own browser | The screensaver between meetings: information, quiet, brand, or the bouncing logo. |`
- Decisions list: add `- The TV shows one page, owned by the agent: the screensaver when idle, the meeting when joined. Providers borrow that page; nothing else ever opens a window on the TV.`
- Known limitations: remove `- One headed browser window opens per configured platform when the agent starts.`
- Implementation notes table: add the row for this spec and plan.

- [ ] **Step 3: Edit the room setup guide**

In the first-run step (the one that says what appears on the TV after `systemctl start croom`), add a sentence: "The TV shows the room's screensaver: pick its style under **TV when idle** on the room page (Information, Quiet, Brand or Bounce). Pressing Join replaces it with the meeting, and Leave brings it back." Rebuild the PDF: `.venv/bin/python docs/guides/crystal-meet-room-setup/build.py`.

- [ ] **Step 4: Run the docs tests and commit**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/docs`
Expected: PASS.

```bash
git add README.md docs/guides/crystal-meet-room-setup/index.html docs/guides/crystal-meet-room-setup.pdf tests/unit/docs/test_readme.py tests/unit/docs/test_room_setup_guide.py
git commit -m "docs: the TV screensaver and the one page on the TV"
```

---

### Task 7: The gate and the acceptance

- [ ] **Step 1: Run the full gate**

Run: `bash /tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/suite-gate.sh tv-screensaver | grep "GATE:"`
Expected: `GATE: PASSED`.

- [ ] **Step 2: Run the agent on this machine once**

Run `.venv/bin/croom -v -c ~/.config/croom/config.yaml` for a minute (WSLg shows the kiosk window): the window must open on the TV page's screensaver, and `curl -s localhost:8080/api/status | grep screensaver` must answer. Stop it with Ctrl-C. Note any warning from the display.

- [ ] **Step 3: Acceptance on PiMeet-3**

After merge: update the Pi (`pip install --force-reinstall --no-deps git+...`), `sudo systemctl restart croom`; the TV shows the screensaver; change the style on the room page; Join a meeting; Leave; the screensaver returns; a Meet join with GPU acceleration on. Record what was proven.
