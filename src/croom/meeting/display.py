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
