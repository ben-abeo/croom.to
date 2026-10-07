"""
The TV display owns the one browser page: parked on the screensaver, lent to a
provider, replaced if it dies (spec 2026-10-07 TV, section 4.3).
"""

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


async def test_show_idle_keeps_trying_until_the_page_is_served(caplog):
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
    assert Config().meeting.kiosk is True
    assert Config.from_dict(config.to_dict()).meeting.kiosk is False
