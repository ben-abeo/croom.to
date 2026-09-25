"""
The loopback web site that hosts the Zoom Meeting SDK page for the room's
browser (spec 2026-09-25 Zoom, sections 4.4 and 4.5): the page, its script,
one-time join parameters, the cross-origin headers Zoom's SDK wants, and a
guard so only this device can talk to it.
"""

import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web

PAGE_DIR = Path(__file__).parent / "zoom_sdk_page"
CROSS_ORIGIN_HEADERS = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "credentialless",
}
LOOPBACK_PEERS = ("127.0.0.1", "::1")
LEFT_PAGE = (
    "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Crystal Meet</title></head>"
    "<body style=\"margin:0;background:#111827;color:#fff;font-family:Lexend,'Segoe UI',Arial,sans-serif;"
    "display:flex;align-items:center;justify-content:center;height:100vh\"><p>Left the meeting.</p></body></html>"
)


class ZoomSdkSite:
    """Serves the SDK page on 127.0.0.1 only; join parameters are handed out once per token."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._host = host
        self._requested_port = port
        self._joins: Dict[str, Dict[str, Any]] = {}
        self._runner: Optional[web.AppRunner] = None
        self.port: Optional[int] = None

    def register_join(self, params: Dict[str, Any]) -> str:
        """Store one join's parameters and return the token the page presents to fetch them once."""
        token = secrets.token_urlsafe(24)
        self._joins[token] = dict(params)
        return token

    def url(self, path: str = "/meeting") -> str:
        return f"http://{self._host}:{self.port}{path}"

    def create_app(self) -> web.Application:
        app = web.Application(middlewares=[self._loopback_only, self._cross_origin])
        app.router.add_get("/meeting", self._page)
        app.router.add_get("/static/meeting.js", self._script)
        app.router.add_get("/join/{token}", self._join)
        app.router.add_get("/left", self._left)
        return app

    @web.middleware
    async def _loopback_only(self, request: web.Request, handler):
        if request.remote not in LOOPBACK_PEERS:
            return web.Response(status=403, text="This page is for the room's own browser.")
        return await handler(request)

    @web.middleware
    async def _cross_origin(self, request: web.Request, handler):
        response = await handler(request)
        response.headers.update(CROSS_ORIGIN_HEADERS)
        return response

    async def _page(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(PAGE_DIR / "meeting.html", headers={"Cache-Control": "no-cache"})

    async def _script(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(PAGE_DIR / "meeting.js", headers={"Cache-Control": "no-cache"})

    async def _join(self, request: web.Request) -> web.Response:
        params = self._joins.pop(request.match_info["token"], None)
        if params is None:
            return web.json_response({"error": "unknown or already used join token"}, status=404)
        return web.json_response(params)

    async def _left(self, request: web.Request) -> web.Response:
        return web.Response(text=LEFT_PAGE, content_type="text/html")

    async def start(self) -> int:
        self._runner = web.AppRunner(self.create_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._requested_port)
        await site.start()
        self.port = self._runner.addresses[0][1]
        return self.port

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self.port = None
