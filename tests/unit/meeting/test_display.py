"""
The TV display owns the one browser page: parked on the screensaver, lent to a
provider, replaced if it dies (spec 2026-10-07 TV, section 4.3).
"""

import asyncio
import logging
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from aiohttp import web

playwright = pytest.importorskip("playwright.async_api")

from croom.core.config import Config  # noqa: E402
from croom.meeting.display import TvDisplay  # noqa: E402

TV_HTML = "<title>Crystal Meet TV</title><h1>Screensaver</h1>"
HEADED = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))   # window chrome is a headed thing
NO_CHROME = "window.outerHeight - window.innerHeight < 20 && window.outerWidth - window.innerWidth < 20"   # a plain window has about 87 px


def a_dead_pid():
    """The pid of a process that has run and been reaped."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


class IdleSite:
    """A stand-in for the control service's /tv page on a loopback port."""

    def __init__(self):
        self.runner = None
        self.port = None

    async def start(self, port=0):
        app = web.Application()
        async def tv(request):
            return web.Response(text=TV_HTML, content_type="text/html")

        app.router.add_get("/tv", tv)
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


async def test_start_parks_the_page_on_the_screensaver():
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


async def test_a_closed_page_is_replaced_and_parked_on_idle():
    """Chromium keeps kiosk mode for its startup window only, so a replacement page would come up in a plain
    window: the whole browser starts again, on a fresh throwaway folder."""
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    try:
        await display.start()
        first = await display.page()
        context, folder = display.context, display.profile_folder
        await first.close()
        second = await display.page()
        assert second is not first and not second.is_closed()
        await second.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
        assert display.context is not context
        assert display.profile_folder != folder and not folder.exists() and display.profile_folder.is_dir()
    finally:
        await display.stop()
        await site.stop()


async def test_the_retry_outlives_its_warning_and_never_shows_an_error_page(caplog):
    site = IdleSite()
    display = TvDisplay("http://127.0.0.1:1/tv", headless=True)   # nothing listens on port 1
    display.RETRY_EVERY_S = 0.1
    display.RETRY_MAX_S = 0.2
    display.RETRY_WARN_AFTER_S = 0.3
    try:
        with caplog.at_level(logging.WARNING):
            await display.start()                                 # must not raise
            await asyncio.sleep(1.0)                              # well past the warning
        assert any("screensaver" in r.getMessage() for r in caplog.records)
        page = await display.page()
        assert not page.url.startswith("chrome-error://")        # no "can't be reached" on the TV
        assert await page.title() == "Crystal Meet"               # the dark holding page
        await site.start()
        display._idle_url = site.url                              # the test moves the site under the display
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
    finally:
        await display.stop()
        if site.runner:
            await site.stop()


async def test_a_crashed_page_is_replaced_and_parked_on_idle():
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    try:
        await display.start()
        first = await display.page()
        try:
            await first.goto("chrome://crash")
        except Exception:  # noqa: BLE001 - the renderer is gone; that is the point
            pass
        second = await display.page()
        assert second is not first and not second.is_closed()
        await second.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
    finally:
        await display.stop()
        await site.stop()


async def test_a_dead_guest_browser_is_started_again():
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    try:
        await display.start()
        first = await display.page()
        await display.context.close()                             # Chromium died underneath us
        page = await display.page()
        assert page is not first and not page.is_closed()
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
    finally:
        await display.stop()
        await site.stop()


async def test_a_dead_profile_browser_is_started_again(tmp_path):
    site = await IdleSite().start()
    display = TvDisplay(site.url, profile_dir=str(tmp_path / "profile"), headless=True)
    try:
        await display.start()
        first = await display.page()
        await display.context.close()                             # the persistent context is gone
        page = await display.page()
        assert page is not first and not page.is_closed()
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
    finally:
        await display.stop()
        await site.stop()


async def test_claim_stops_a_pending_re_park(tmp_path):
    meeting = tmp_path / "meeting.html"
    meeting.write_text("<title>Meeting</title>")
    site = IdleSite()
    display = TvDisplay("http://127.0.0.1:1/tv", headless=True)
    display.RETRY_EVERY_S = 0.2
    try:
        await display.start()                                     # nothing listens: a retry is pending
        assert display.parked
        page = await display.claim()                              # a provider takes the page for a meeting
        assert not display.parked
        await page.goto(meeting.as_uri())
        await site.start()
        display._idle_url = site.url                              # the screensaver comes up during the meeting
        await asyncio.sleep(1.0)
        assert await page.title() == "Meeting"                    # the meeting was not undone
        await display.show_idle()
        assert display.parked
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
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


async def test_a_guest_browser_runs_on_a_throwaway_profile_folder_that_leaves_with_it():
    """Chromium keeps kiosk mode for its startup window only, and only a persistent context opens one: a guest gets
    a private folder of its own for as long as the browser runs, and nothing of it survives a stop."""
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    try:
        await display.start()
        folder = display.profile_folder
        assert display.profile_dir is None and folder is not None
        assert folder.name.startswith("croom-tv-") and oct(folder.stat().st_mode & 0o777) == "0o700"
        page = await display.page()
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
        assert (folder / "Default").is_dir()                     # Chromium runs on it: the page is its startup window's
    finally:
        await display.stop()
        await site.stop()
    assert not folder.exists()


@pytest.mark.skipif(not HEADED, reason="needs a display: window chrome is a headed thing")
async def test_a_guest_browser_in_kiosk_mode_has_no_window_chrome_even_after_its_page_dies():
    """What the TV Pi showed: the guest browser's page came up with a tab strip and an address bar, --kiosk or not,
    and so would a replacement page after the first one died. Measured as the window's outer size against its inner."""
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=False)
    try:
        try:
            await display.start()
        except Exception as e:  # noqa: BLE001 - a DISPLAY that names nothing
            pytest.skip(f"no usable display for a headed browser: {e!r}")
        page = await display.page()
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
        await page.wait_for_function(NO_CHROME, timeout=5000)
        await page.close()
        page = await display.page()
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
        await page.wait_for_function(NO_CHROME, timeout=5000)
    finally:
        await display.stop()
        await site.stop()


async def test_two_callers_finding_the_page_gone_share_one_restart(monkeypatch, tmp_path):
    """A double tap, or a tablet and the idle retry, both meeting a dead page: one restart, one browser, the same
    fresh page for both (a second Chromium on the signed-in profile is refused; a guest would leak one)."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))    # the throwaway folders go here
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    try:
        await display.start()
        first = await display.page()
        context = display.context
        await first.close()
        a, b = await asyncio.gather(display.page(), display.claim())
        assert a is b and not a.is_closed() and display.context is not context
        assert len(list(tmp_path.glob("croom-tv-*"))) == 1       # one browser, one folder
        await a.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
    finally:
        await display.stop()
        await site.stop()


async def test_a_launch_that_fails_leaves_nothing_behind_and_is_tried_again(monkeypatch, tmp_path):
    """A launch timeout on a loaded Pi, or X not ready: the driver and the folder go, the call fails, and the
    display brings the browser back on its own; a call in the meantime tries again instead of refusing."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    display.RETRY_MAX_S = 0.3
    real_launch, failures = display._launch, {"left": 0}

    async def flaky(folder):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("Chromium would not start")
        return await real_launch(folder)

    monkeypatch.setattr(display, "_launch", flaky)
    try:
        await display.start()
        first = await display.page()
        await first.close()
        failures["left"] = 1
        with pytest.raises(RuntimeError, match="would not start"):
            await display.page()
        assert display.context is None and display._playwright is None and display.profile_folder is None
        assert not list(tmp_path.glob("croom-tv-*"))
        for _ in range(50):                                      # the background retry, 0.3 s after the failure
            if display.context is not None and display._page is not None and not display._page.is_closed():
                break
            await asyncio.sleep(0.1)
        assert display.context is not None, "the browser did not come back on its own"
        page = await display.page()
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
        assert len(list(tmp_path.glob("croom-tv-*"))) == 1
    finally:
        await display.stop()
        await site.stop()
    assert not list(tmp_path.glob("croom-tv-*"))


async def test_stale_throwaway_folders_are_swept_at_start_and_live_ones_kept(monkeypatch, tmp_path):
    """An agent killed without stop() (SIGKILL, a crash) leaves its folder; the next start removes it, and only it."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    long_ago, dead = (time.time() - 600,) * 2, a_dead_pid()
    stale = tmp_path / f"croom-tv-{dead}-abc"
    stale.mkdir()
    (stale / "Default").mkdir()
    os.utime(stale, long_ago)
    young = tmp_path / f"croom-tv-{dead}-young"                   # too young to tell: a browser may be starting on it
    young.mkdir()
    mine = tmp_path / f"croom-tv-{os.getpid()}-live"             # this agent's own, however old
    mine.mkdir()
    os.utime(mine, long_ago)
    held = tmp_path / f"croom-tv-{dead}-held"                     # a live Chromium holds it
    held.mkdir()
    (held / "SingletonLock").symlink_to(f"{socket.gethostname()}-{os.getpid()}")
    os.utime(held, long_ago)
    foreign = tmp_path / "croom-tv-oldstyle"                      # not named by us
    foreign.mkdir()
    os.utime(foreign, long_ago)
    odd = [tmp_path / "croom-tv-99999999999999999999-x", tmp_path / "croom-tv-\u00b2-x"]   # no pid reads like that
    for folder in odd:
        folder.mkdir()
        os.utime(folder, long_ago)
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    try:
        await display.start()                                    # the odd names never stop the TV from starting
        assert not stale.exists()
        assert young.is_dir() and mine.is_dir() and held.is_dir() and foreign.is_dir() and all(f.is_dir() for f in odd)
        own = display.profile_folder
        assert own.parent == tmp_path and own.name.startswith(f"croom-tv-{os.getpid()}-")
    finally:
        await display.stop()
        await site.stop()
    assert not own.exists() and young.is_dir() and mine.is_dir() and held.is_dir() and foreign.is_dir()


@pytest.mark.parametrize("long_up", [True, False])
async def test_a_parked_page_that_crashes_brings_a_fresh_browser_on_its_own(long_up):
    """An idle TV does not sit on the desktop until the next Join: the crash handler restarts the browser, at
    once when the browser has been up a while, after a pause when it had only just started (a screensaver
    that crashes on every load must not keep a Pi relaunching Chromium)."""
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    display.RETRY_MAX_S = 0.5
    try:
        await display.start()
        page = await display.page()
        context = display.context
        if long_up:
            display._started_at -= 60                            # the browser has been up for a minute
        else:
            display.RETRY_MAX_S = 2.0                            # a crash right after the start waits this long
        try:
            await page.goto("chrome://crash")
        except Exception:  # noqa: BLE001 - the renderer is gone; that is the point
            pass
        crashed_at = asyncio.get_running_loop().time()
        for _ in range(100):                                     # no call to page() from here
            if display.context is not context and display._page is not None and not display._page.is_closed():
                break
            await asyncio.sleep(0.1)
        assert display.context is not context, "no fresh browser came on its own"
        await display._page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
        assert display.parked
        waited = asyncio.get_running_loop().time() - crashed_at
        assert (waited >= 1.5) == (not long_up), f"the fresh browser came after {waited:.1f} s"
    finally:
        await display.stop()
        await site.stop()


async def test_a_claim_during_a_restart_waits_for_the_page_to_be_parked_first(monkeypatch):
    """A Join landing in the tail of a restart (page adopted, screensaver not shown yet) must not get the page
    and then have the restart's show_idle() park it under the provider."""
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    adopted, real_dark = asyncio.Event(), display._show_dark

    async def slow_dark():
        await real_dark()
        adopted.set()                                            # the page is up and quiet; not parked yet
        await asyncio.sleep(0.5)                                 # a wide tail

    monkeypatch.setattr(display, "_show_dark", slow_dark)
    try:
        await display.start()
        first = await display.page()
        adopted.clear()
        await first.close()
        restart = asyncio.create_task(display.page())
        await adopted.wait()                                     # inside the tail
        claimed = await display.claim()
        await restart
        assert claimed is display._page and not display.parked  # the claim came after the parking, and holds
        await asyncio.sleep(0.3)
        assert not display.parked
    finally:
        await display.stop()
        await site.stop()


async def test_a_caller_cancelled_during_a_restart_leaves_nothing_behind_and_the_browser_comes_back(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    display.RETRY_MAX_S = 0.3
    real_launch = display._launch

    async def slow_launch(folder):
        await asyncio.sleep(0.4)
        return await real_launch(folder)

    try:
        await display.start()
        first = await display.page()
        await first.close()
        monkeypatch.setattr(display, "_launch", slow_launch)
        claim = asyncio.create_task(display.claim())
        await asyncio.sleep(0.15)                                # inside the launch
        claim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await claim
        assert display.context is None and display._playwright is None and not list(tmp_path.glob("croom-tv-*"))
        for _ in range(50):                                      # the retry brings the browser back
            if display.context is not None and display._page is not None and not display._page.is_closed():
                break
            await asyncio.sleep(0.1)
        assert display.context is not None, "the browser did not come back on its own"
        page = await display.page()
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
    finally:
        await display.stop()
        await site.stop()
    assert not list(tmp_path.glob("croom-tv-*"))


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
    assert Config().meeting.kiosk is True
    assert Config.from_dict(config.to_dict()).meeting.kiosk is False


def test_from_config_uses_the_control_host_when_it_is_not_a_wildcard():
    assert TvDisplay.from_config(Config.from_dict({"control": {"host": "192.168.1.5", "port": 8080}})).idle_url == "http://192.168.1.5:8080/tv"
    assert TvDisplay.from_config(Config.from_dict({"control": {"host": "0.0.0.0"}})).idle_url == "http://127.0.0.1:8080/tv"
    assert TvDisplay.from_config(Config.from_dict({"control": {"host": "::"}})).idle_url == "http://127.0.0.1:8080/tv"


def test_browser_args_hide_the_crash_restore_bubble():
    args = TvDisplay("about:blank").browser_args()
    assert "--disable-session-crashed-bubble" in args and "--hide-crash-restore-bubble" in args


def test_context_options_do_not_fix_the_viewport():
    """The page fills the TV at whatever resolution the Pi drives it; nothing is emulated."""
    options = TvDisplay.context_options()
    assert "viewport" not in options
    assert options["no_viewport"] is True
    assert options["permissions"] == ["camera", "microphone"]


async def test_the_page_fills_the_window_whatever_its_size():
    site = await IdleSite().start()
    display = TvDisplay(site.url, headless=True)
    display.BASE_ARGS = [a for a in TvDisplay.BASE_ARGS if not a.startswith("--window-size")] + ["--window-size=1280,720"]
    try:
        await display.start()
        page = await display.page()
        await page.wait_for_function("document.title === 'Crystal Meet TV'", timeout=5000)
        assert await page.evaluate("[window.innerWidth, window.innerHeight]") == [1280, 720]
    finally:
        await display.stop()
        await site.stop()
