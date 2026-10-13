"""
The TV: one browser window, one page, owned here and lent to the meeting
providers (spec 2026-10-07 TV screensaver, section 4.3). Idle, the page sits on
the control service's /tv screensaver; a provider claims it for a meeting and
hands it back through show_idle(). If the page or the browser dies, page()
starts a new browser; a parked page that crashes brings one on its own.

The browser is always a persistent context, on the signed-in Google profile
when one is configured and otherwise on a throwaway folder that lives as long
as the browser: Chromium keeps kiosk mode (--kiosk) for the window it opens at
startup only, and only a persistent context opens one. A launch() browser
makes its pages through the automation protocol, in plain windows with a tab
strip and an address bar whatever the flags say, and so does a page made to
replace one that died once its window is gone. That is what the TV Pi showed.
"""

import asyncio
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from croom.meeting.browser_env import profile_holder

logger = logging.getLogger(__name__)

try:
    from playwright.async_api import BrowserContext, Page, async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False

# What the TV shows until the screensaver answers: Crystal PM navy, nothing else.
DARK_PAGE = "data:text/html,<title>Crystal Meet</title><body style='margin:0;background:%23001636'></body>"
WILDCARD_HOSTS = ("", "0.0.0.0", "::")
THROWAWAY_PREFIX = "croom-tv-"   # a guest's profile folder: croom-tv-<pid of the agent>-<random>, in the temp folder


def _alive_pid(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # alive, owned by someone else
    return True


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
    STALE_AFTER_S = 60.0      # a throwaway folder younger than this may belong to a browser still starting up

    def __init__(self, idle_url: str, profile_dir: Optional[str] = None, headless: bool = False, kiosk: bool = True):
        self._idle_url = idle_url
        self._profile_dir: Optional[Path] = Path(profile_dir) if profile_dir else None
        self._headless = headless
        self._kiosk = kiosk
        self._playwright = None
        self._throwaway: Optional[Path] = None   # a guest's profile folder, made at start and removed at stop
        self._context: Optional["BrowserContext"] = None
        self._page: Optional["Page"] = None
        self._retry: Optional[asyncio.Task] = None
        self._parked = False   # True while the page belongs to the screensaver, False while a provider has it
        self._wanted = False   # True from start() to stop(): page() may start the browser again
        self._lock = asyncio.Lock()   # one start, stop or restart at a time
        self._started_at = 0.0        # loop time of the last launch, for the back-off after a crash
        self._tasks: set = set()      # fire-and-forget tasks, held so they are not collected mid-flight

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
        """The configured, signed-in profile folder; None for a guest."""
        return self._profile_dir

    @property
    def profile_folder(self) -> Optional[Path]:
        """The folder the running browser keeps its profile in: the configured one, or a guest's throwaway."""
        return self._profile_dir if self._profile_dir is not None else self._throwaway

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
        async with self._lock:
            self._wanted = True
            await self._start()

    async def stop(self) -> None:
        async with self._lock:
            self._wanted = False
            await self._stop()

    async def _start(self) -> None:
        """Lock held. A launch that fails leaves nothing behind, and the display stays wanted: page() tries again."""
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")
        folder = self._profile_dir
        if folder is None:
            self._sweep_stale_folders()
            try:
                folder = Path(tempfile.mkdtemp(prefix=f"{THROWAWAY_PREFIX}{os.getpid()}-"))   # private from the start
            except OSError as e:
                raise RuntimeError(f"No room for a browser profile folder in {tempfile.gettempdir()}: {e}") from e
            self._throwaway = folder
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(folder, 0o700)
        except OSError as e:
            await self._stop()
            raise RuntimeError(f"Browser profile folder {folder} is not usable: {e}") from e
        try:
            self._playwright = await async_playwright().start()
            self._context = await self._launch(folder)
            self._adopt(self._context.pages[0] if self._context.pages else await self._context.new_page())
        except BaseException:   # a failed launch, or a caller cancelled while waiting for it
            await self._stop()   # the driver and a guest's folder go with it
            raise
        self._started_at = asyncio.get_running_loop().time()
        if self._profile_dir is not None:
            logger.info(f"TV browser started on the signed-in profile at {self._profile_dir}")
        else:
            logger.info(f"TV browser started as a guest on {folder}")
        await self._show_dark()
        await self.show_idle()

    async def _launch(self, folder: Path) -> "BrowserContext":
        return await self._playwright.chromium.launch_persistent_context(
            str(folder), headless=self._headless, args=self.browser_args(), **self.context_options(),
        )

    async def _stop(self) -> None:
        """Lock held. Closes whatever is open and removes a guest's folder; a half-started display is fine."""
        self._cancel_retry()
        if self._context is not None:
            try:
                await asyncio.wait_for(self._context.close(), timeout=self.CLOSE_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001 - a hung browser must not hang the agent
                logger.warning(f"TV browser did not close cleanly: {e!r}")
            self._context = None
        self._page = None
        self._parked = False
        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=self.CLOSE_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Playwright did not stop cleanly: {e!r}")
            self._playwright = None
        if self._throwaway is not None:
            folder, self._throwaway = self._throwaway, None
            await asyncio.to_thread(shutil.rmtree, folder, ignore_errors=True)   # a guest leaves nothing behind

    @classmethod
    def _sweep_stale_folders(cls) -> None:
        """Throwaway folders an agent left behind by dying without stop() (SIGKILL, a crash). A folder stays
        while the agent that made it (its pid is in the name) or a Chromium holding it is alive, or while it is
        too young to tell."""
        now = time.time()
        for folder in Path(tempfile.gettempdir()).glob(f"{THROWAWAY_PREFIX}*"):
            try:
                maker, sep, _ = folder.name[len(THROWAWAY_PREFIX):].partition("-")
                if not sep or not maker.isdecimal() or len(maker) > 8:
                    continue   # not one of ours
                pid = int(maker)
                if (pid == os.getpid() or _alive_pid(pid) or profile_holder(folder) is not None
                        or now - folder.stat().st_mtime < cls.STALE_AFTER_S):
                    continue
                shutil.rmtree(folder)
                logger.info(f"Removed the browser profile folder a dead agent left behind: {folder}")
            except Exception as e:  # noqa: BLE001 - a folder that cannot be judged or removed never stops the TV
                logger.debug(f"Left {folder} alone: {e!r}")

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # The page
    # ------------------------------------------------------------------

    def _adopt(self, page: "Page") -> None:
        self._page = page
        page.on("crash", self._on_crash)

    def _on_crash(self, page: "Page") -> None:
        """Chromium's renderer died ("Aw, Snap!"): forget the page, so the next page() brings a fresh browser;
        a parked page does not wait for that call."""
        if page is self._page:
            logger.warning("The TV page crashed; a fresh browser will replace it")
            self._page = None
            if self._parked:
                self._spawn(self._recover())
        self._spawn(self._close_quietly(page))

    async def _recover(self) -> None:
        """A parked page crashed: a fresh browser now, or after a pause when the last one is itself recent (a
        screensaver that crashes on every load must not keep a Pi relaunching Chromium)."""
        if not self._wanted or not self._parked:
            return
        if self._retry is not None and not self._retry.done():
            return   # the retry task brings the browser at its next round
        if asyncio.get_running_loop().time() - self._started_at < self.RETRY_MAX_S:
            self._retry = asyncio.create_task(self._retry_idle(self.RETRY_MAX_S))
            return
        try:
            await self.page()
        except Exception as e:  # noqa: BLE001 - page() has arranged the next try
            if self._wanted:
                logger.warning(f"The TV browser could not be started again after a crash ({e!r})")

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
            logger.warning(f"The TV page does not answer ({e!r}); a fresh browser will replace it")
            return False

    async def page(self) -> "Page":
        """The shared page; a fresh browser's, parked on the screensaver, when the old one is gone.

        A replacement page in the old browser would open in a plain window once the page's own window is
        gone (Chromium keeps kiosk mode for its startup window only), so the whole browser starts again. One
        restart at a time: a second caller that finds the page gone waits for it and gets the same fresh page."""
        if not self._wanted:
            raise RuntimeError("TV display not started")
        if not self._lock.locked() and await self._alive():   # a restart hands its page out once it is parked
            return self._page
        async with self._lock:
            if not self._wanted:
                raise RuntimeError("TV display not started")
            if await self._alive():
                return self._page
            logger.warning("The TV page is gone; starting the browser again")
            await self._stop()
            try:
                await self._start()
            finally:
                if self._context is None and self._wanted:   # the launch failed, or the caller was cancelled
                    self._cancel_retry()
                    self._retry = asyncio.create_task(self._retry_idle(self.RETRY_MAX_S))   # the TV comes back on its own
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

    async def _retry_idle(self, delay: Optional[float] = None) -> None:
        """Keep trying for as long as the page is parked or the browser is down; one warning when it takes long."""
        started = asyncio.get_running_loop().time()
        delay = delay or self.RETRY_EVERY_S
        warned = False
        while self._wanted and (self._parked or self._context is None):
            await asyncio.sleep(delay)
            if not self._wanted or (self._context is not None and not self._parked):
                return   # stopped, or a provider has the page
            if self._page is None or self._page.is_closed():
                try:
                    await self.page()   # a fresh browser, parked on the screensaver by its own start ...
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"The TV browser could not be started again ({e!r}); "
                                   f"trying again in {self.RETRY_MAX_S:.0f} s")
                return   # ... or, when its launch failed, page() has arranged the next try
            if await self._try_idle():
                return
            if not warned and asyncio.get_running_loop().time() - started >= self.RETRY_WARN_AFTER_S:
                logger.warning(f"The TV could not load the screensaver at {self._idle_url} "
                               f"for {self.RETRY_WARN_AFTER_S:.0f} s; still trying")
                warned = True
            delay = min(delay * 2, self.RETRY_MAX_S)
