"""
With a profile folder the Meet provider keeps its browser session there, so a
one-time Google sign-in survives restarts (spec 2026-10-07, section 4.3).
"""

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.providers.google_meet import GoogleMeetProvider  # noqa: E402


def page_file(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<!DOCTYPE html><html><head><title>Crystal Meet</title></head><body>hello</body></html>")
    return page


async def test_profile_launch_creates_a_private_folder_and_a_working_page(tmp_path):
    profile = tmp_path / "profile"
    provider = GoogleMeetProvider(profile_dir=str(profile), room_name="Room 3", headless=True)
    await provider.initialize()
    try:
        assert oct(profile.stat().st_mode & 0o777) == "0o700"
        await provider._page.goto(page_file(tmp_path).as_uri())
        assert await provider._page.title() == "Crystal Meet"
        assert provider._browser is None  # a persistent context owns its browser
    finally:
        await provider.shutdown()
    assert provider._context is None and provider._playwright is None
    assert any(profile.iterdir())  # Chromium wrote the profile there


async def test_guest_launch_still_works_without_a_profile(tmp_path):
    provider = GoogleMeetProvider(headless=True)
    await provider.initialize()
    try:
        await provider._page.goto(page_file(tmp_path).as_uri())
        assert await provider._page.title() == "Crystal Meet"
        assert provider._browser is not None
    finally:
        await provider.shutdown()


async def test_unusable_profile_folder_is_a_clear_error(tmp_path):
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    provider = GoogleMeetProvider(profile_dir=str(parent / "profile"), headless=True)
    try:
        with pytest.raises(RuntimeError) as failure:
            await provider.initialize()
        assert str(parent / "profile") in str(failure.value)
    finally:
        await provider.shutdown()
        parent.chmod(0o700)
