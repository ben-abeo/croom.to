"""
The TV: one browser window, one page, owned here and lent to the meeting
providers (spec 2026-10-07 TV screensaver, section 4.3). Idle, the page sits on
the control service's /tv screensaver; a provider claims it for a meeting and
hands it back through show_idle(). If the page dies, page() replaces it; if
the whole browser dies, page() starts a new one.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False

# What the TV shows until the screensaver answers: Crystal PM navy, nothing else.
DARK_PAGE = "data:text/html,<title>Crystal Meet</title><body style='margin:0;background:%23001636'></body>"
WILDCARD_HOSTS = ("", "0.0.0.0", "::")


class TvDisplay:
    BASE_ARGS = [
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-infobars",
        "--disable-session-crashed-bubble",   # no "Restore pages?" on the TV after an unclean stop
        "--hide-crash-restore-bubble",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1920,1080",   # the window when kiosk is off; kiosk fills the screen
    ]
    RETRY_EVERY_S = 2.0       # first retry interval while the control service is still coming up
    RETRY_MAX_S = 10.0        # the interval grows to this and stays there
    RETRY_WARN_AFTER_S = 30.0  # one warning once the screensaver has been unreachable this long
    PROBE_TIMEOUT_S = 1.0
    CLOSE_TIMEOUT_S = 5.0
    LIVENESS_TIMEOUT_MS = 2000

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
        self._parked = False   # True while the page belongs to the screensaver, False while a provider has it

    @classmethod
    def from_config(cls, config) -> "TvDisplay":
        host = config.control.host if config.control.host not in WILDCARD_HOSTS else "127.0.0.1"
        idle_url = f"http://{host}:{config.control.port}/tv" if config.control.enabled else "about:blank"
        return cls(idle_url, profile_dir=config.meeting.google_profile_dir or None, kiosk=config.meeting.kiosk)

    @classmethod
    def context_options(cls) -> dict:
        """No fixed viewport: the page fills the window, so the TV is driven at whatever resolution
        the Pi outputs (1080p or 4K alike) instead of a 1920x1080 area in its corner."""
        return {"permissions": ["camera", "microphone"], "no_viewport": True}

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

    @property
    def parked(self) -> bool:
        return self._parked

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

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
        self._adopt(self._context.pages[0] if self._context.pages else await self._context.new_page())
        await self._show_dark()
        await self.show_idle()

    async def stop(self) -> None:
        self._cancel_retry()
        for attribute in ("_context", "_browser"):
            closer = getattr(self, attribute)
            if closer is not None:
                try:
                    await asyncio.wait_for(closer.close(), timeout=self.CLOSE_TIMEOUT_S)
                except Exception as e:  # noqa: BLE001 - a hung browser must not hang the agent
                    logger.warning(f"TV browser {attribute[1:]} did not close cleanly: {e}")
                setattr(self, attribute, None)
        self._page = None
        self._parked = False
        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=self.CLOSE_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Playwright did not stop cleanly: {e}")
            self._playwright = None

    async def _restart(self) -> None:
        """The browser itself is gone: start a fresh one on the same settings."""
        logger.warning("The TV browser is gone; starting a new one")
        await self.stop()
        await self.start()

    # ------------------------------------------------------------------
    # The page
    # ------------------------------------------------------------------

    def _adopt(self, page: "Page") -> None:
        self._page = page
        page.on("crash", self._on_crash)

    def _on_crash(self, page: "Page") -> None:
        """Chromium's renderer died ("Aw, Snap!"): forget the page so the next page() replaces it."""
        if page is self._page:
            logger.warning("The TV page crashed; it will be replaced")
            self._page = None
        asyncio.ensure_future(self._close_quietly(page))

    @staticmethod
    async def _close_quietly(page: "Page") -> None:
        try:
            await page.close()
        except Exception:  # noqa: BLE001 - a crashed page may refuse
            pass

    async def _alive(self) -> bool:
        """Whether the current page still answers; a crashed renderer does not."""
        if self._page is None or self._page.is_closed():
            return False
        try:
            await asyncio.wait_for(self._page.evaluate("1"), timeout=self.LIVENESS_TIMEOUT_MS / 1000)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"The TV page does not answer ({e}); it will be replaced")
            return False

    async def page(self) -> "Page":
        """The shared page; a new one, parked on the screensaver, when the old one is gone."""
        if self._context is None:
            raise RuntimeError("TV display not started")
        if await self._alive():
            return self._page
        dead = self._page
        self._page = None
        if dead is not None and not dead.is_closed():
            await self._close_quietly(dead)
        try:
            page = await self._context.new_page()
        except Exception:  # noqa: BLE001 - the context or browser is gone
            await self._restart()
            return self._page
        logger.warning("The TV page was closed; opened a new one")
        self._adopt(page)
        await self.show_idle()
        return self._page

    async def claim(self) -> "Page":
        """The page for a meeting: nothing parks it again until show_idle() is called."""
        page = await self.page()
        self._cancel_retry()
        self._parked = False
        return page

    # ------------------------------------------------------------------
    # The screensaver
    # ------------------------------------------------------------------

    async def show_idle(self) -> None:
        """Park the page on the screensaver; keep trying in the background while the control service is not up."""
        self._cancel_retry()
        self._parked = True
        if await self._try_idle():
            return
        self._retry = asyncio.create_task(self._retry_idle())

    def _cancel_retry(self) -> None:
        if self._retry is not None and not self._retry.done() and self._retry is not asyncio.current_task():
            self._retry.cancel()
        self._retry = None

    async def _show_dark(self) -> None:
        try:
            await self._page.goto(DARK_PAGE, wait_until="commit")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Could not show the holding page: {e}")

    async def _reachable(self) -> bool:
        """A TCP probe, so a refused connection never puts Chromium's error page on the TV."""
        parts = urlsplit(self._idle_url)
        if parts.scheme not in ("http", "https"):
            return True
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)),
                timeout=self.PROBE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - not listening yet
            return False
        writer.close()
        return True

    async def _try_idle(self) -> bool:
        if self._page is None or self._page.is_closed():
            return False
        if not await self._reachable():
            return False
        try:
            await self._page.goto(self._idle_url, wait_until="commit")
            return True
        except Exception as e:  # noqa: BLE001 - the control service may not be listening yet
            logger.debug(f"Screensaver not reachable yet at {self._idle_url}: {e}")
            return False

    async def _retry_idle(self) -> None:
        """Keep trying for as long as the page is parked; one warning when it takes long."""
        started = asyncio.get_running_loop().time()
        delay = self.RETRY_EVERY_S
        warned = False
        while self._parked:
            await asyncio.sleep(delay)
            if not self._parked:
                return
            if self._page is None or self._page.is_closed():
                await self.page()   # replaces the page and parks it (or restarts the browser)
                return
            if await self._try_idle():
                return
            if not warned and asyncio.get_running_loop().time() - started >= self.RETRY_WARN_AFTER_S:
                logger.warning(f"The TV could not load the screensaver at {self._idle_url} "
                               f"for {self.RETRY_WARN_AFTER_S:.0f} s; still trying")
                warned = True
            delay = min(delay * 2, self.RETRY_MAX_S)
