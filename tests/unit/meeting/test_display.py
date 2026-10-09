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

TV_HTML = "<title>Crystal Meet TV</title><h1>Screensaver</h1>"


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
        await display._browser.close()                            # Chromium died underneath us
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
