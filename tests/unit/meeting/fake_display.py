"""A display stand-in for provider tests that drive a page they create themselves."""

from pathlib import Path
from typing import Optional


class FakeDisplay:
    def __init__(self, page=None, profile_dir: Optional[str] = None, idle_url: str = "about:blank"):
        self._page = page
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self.idle_url = idle_url
        self.shown = 0
        self.claimed = 0
        self.started = 0
        self.stopped = 0
        self.context = getattr(page, "context", None)

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def page(self):
        return self._page

    async def claim(self):
        self.claimed += 1
        return self._page

    async def show_idle(self):
        self.shown += 1
        if self._page is not None and not self._page.is_closed():
            await self._page.goto(self.idle_url)
