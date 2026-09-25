"""
The loopback site that hosts the Zoom SDK page (spec 2026-09-25 Zoom, section
4.4): page and script served, one-time join parameters, cross-origin headers,
loopback only.
"""

from unittest import mock

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from croom.meeting.providers.zoom_sdk_site import CROSS_ORIGIN_HEADERS, PAGE_DIR, SDK_VERSION, ZoomSdkSite


async def client_for(site):
    client = TestClient(TestServer(site.create_app()))
    await client.start_server()
    return client


async def test_serves_the_page_and_script_with_cross_origin_headers():
    client = await client_for(ZoomSdkSite())
    try:
        page = await client.get("/meeting")
        assert page.status == 200 and page.headers["Content-Type"].startswith("text/html")
        body = await page.text()
        assert "source.zoom.us/6.5.0/zoom-meeting-6.5.0.min.js" in body and 'src="/static/meeting.js"' in body
        for name, value in CROSS_ORIGIN_HEADERS.items():
            assert page.headers[name] == value
        script = await client.get("/static/meeting.js")
        assert script.status == 200 and "crystalMeet" in await script.text()
        left = await client.get("/left")
        assert left.status == 200 and "Left the meeting" in await left.text()
    finally:
        await client.close()


async def test_join_parameters_are_served_once():
    site = ZoomSdkSite()
    token = site.register_join({"meetingNumber": "123", "signature": "sig", "userName": "Room 1"})
    client = await client_for(site)
    try:
        first = await client.get(f"/join/{token}")
        assert first.status == 200 and await first.json() == {"meetingNumber": "123", "signature": "sig", "userName": "Room 1"}
        second = await client.get(f"/join/{token}")
        assert second.status == 404
        assert (await client.get("/join/never-issued")).status == 404
    finally:
        await client.close()


async def test_tokens_are_unguessable_and_distinct():
    site = ZoomSdkSite()
    tokens = {site.register_join({"n": i}) for i in range(20)}
    assert len(tokens) == 20 and all(len(t) >= 24 for t in tokens)


async def test_other_peers_are_refused():
    site = ZoomSdkSite()
    transport = mock.Mock()
    transport.get_extra_info = lambda name, default=None: ("10.0.0.5", 51000) if name == "peername" else default
    request = make_mocked_request("GET", "/meeting", transport=transport)
    called = []

    async def handler(req):
        called.append(req)
        raise AssertionError("handler must not run")

    response = await site._loopback_only(request, handler)
    assert response.status == 403 and called == []


async def test_start_binds_an_ephemeral_loopback_port():
    site = ZoomSdkSite()
    port = await site.start()
    try:
        assert port == site.port and 1024 < port < 65536
        assert site.url() == f"http://127.0.0.1:{port}/meeting"
    finally:
        await site.stop()
    assert site.port is None


async def test_the_sdk_version_is_pinned_once_and_injected_into_the_page():
    raw = (PAGE_DIR / "meeting.html").read_text(encoding="utf-8")
    assert SDK_VERSION not in raw and "__SDK_VERSION__" in raw
    assert "onerror" in raw
    client = await client_for(ZoomSdkSite())
    try:
        body = await (await client.get("/meeting")).text()
        assert "__SDK_VERSION__" not in body
        assert f"source.zoom.us/{SDK_VERSION}/zoom-meeting-{SDK_VERSION}.min.js" in body
        assert f'<meta name="sdk-version" content="{SDK_VERSION}">' in body
    finally:
        await client.close()
