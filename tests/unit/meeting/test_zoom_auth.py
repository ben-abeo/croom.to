"""
Zoom credentials, the not-configured rule, the Meeting SDK signature and the
Server-to-Server client that fetches a room user's ZAK (spec 2026-09-25 Zoom,
sections 4.2 and 4.3). Zoom itself is never contacted: a local aiohttp app
stands in for it.
"""

import base64
import hashlib
import hmac
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from croom.core.config import Config
from croom.meeting.zoom_auth import (
    ZoomApi,
    ZoomAuthError,
    ZoomCredentials,
    load_zoom_credentials,
    meeting_sdk_signature,
    zoom_not_configured_reason,
)

FULL = {
    "sdk_client_id": "sdkClient123",
    "sdk_client_secret": "sdkSecret456",
    "account_id": "acct789",
    "s2s_client_id": "s2sClient",
    "s2s_client_secret": "s2sSecret",
    "room_user": "room1@crystalpm.com",
}
SDK_ONLY = {"sdk_client_id": "sdkClient123", "sdk_client_secret": "sdkSecret456"}


def write(tmp_path, data, name="zoom-credentials.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return str(path)


def b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class TestConfigField:
    def test_parses_and_round_trips(self):
        config = Config.from_dict({"meeting": {"platforms": ["zoom"], "zoom_credentials_path": "/etc/croom/zoom-credentials.json"}})
        assert config.meeting.zoom_credentials_path == "/etc/croom/zoom-credentials.json"
        assert config.to_dict()["meeting"]["zoom_credentials_path"] == "/etc/croom/zoom-credentials.json"

    def test_defaults_to_empty(self):
        assert Config().meeting.zoom_credentials_path == ""


class TestNotConfiguredRule:
    def test_ready_with_the_full_file(self, tmp_path):
        assert zoom_not_configured_reason(write(tmp_path, FULL)) is None

    def test_ready_with_the_sdk_part_only(self, tmp_path):
        assert zoom_not_configured_reason(write(tmp_path, SDK_ONLY)) is None

    def test_no_path(self):
        assert zoom_not_configured_reason("") == "no zoom_credentials_path in the config"

    def test_missing_file(self, tmp_path):
        reason = zoom_not_configured_reason(str(tmp_path / "nope.json"))
        assert reason.startswith("credentials file not found or unreadable: ")

    def test_not_json(self, tmp_path):
        assert zoom_not_configured_reason(write(tmp_path, "not json")).startswith("credentials file is not a JSON object")

    def test_sdk_placeholder(self, tmp_path):
        data = dict(FULL, sdk_client_secret="REPLACE_WITH_SDK_SECRET")
        assert zoom_not_configured_reason(write(tmp_path, data)).startswith("sdk_client_secret is missing or still REPLACE")

    def test_partial_server_to_server_part(self, tmp_path):
        data = dict(FULL)
        del data["room_user"]
        reason = zoom_not_configured_reason(write(tmp_path, data))
        assert reason.startswith("room_user missing or still REPLACE") and "go together" in reason


class TestLoad:
    def test_full(self, tmp_path):
        creds = load_zoom_credentials(write(tmp_path, FULL))
        assert creds == ZoomCredentials(**FULL)
        assert creds.has_room_user is True

    def test_sdk_only(self, tmp_path):
        creds = load_zoom_credentials(write(tmp_path, SDK_ONLY))
        assert creds.has_room_user is False and creds.room_user == ""

    def test_reason_becomes_an_error(self, tmp_path):
        with pytest.raises(ZoomAuthError) as failure:
            load_zoom_credentials(str(tmp_path / "gone.json"))
        assert "not found or unreadable" in str(failure.value)


class TestSignature:
    def test_decodes_with_the_secret_and_carries_the_claims(self):
        token = meeting_sdk_signature("sdkClient123", "sdkSecret456", 99612060433, now=1_800_000_000)
        header, payload, signature = token.split(".")
        expected = hmac.new(b"sdkSecret456", f"{header}.{payload}".encode(), hashlib.sha256).digest()
        assert b64url_decode(signature) == expected
        assert json.loads(b64url_decode(header)) == {"alg": "HS256", "typ": "JWT"}
        claims = json.loads(b64url_decode(payload))
        assert claims == {  # iat is backdated 30 s so a room clock slightly ahead of Zoom's still passes
            "appKey": "sdkClient123", "sdkKey": "sdkClient123", "mn": "99612060433", "role": 0,
            "iat": 1_799_999_970, "exp": 1_800_007_170, "tokenExp": 1_800_007_170,
        }
        assert "=" not in token

    def test_custom_role_and_ttl(self):
        claims = json.loads(b64url_decode(meeting_sdk_signature("a", "b", "123", role=1, now=100, ttl_seconds=1800).split(".")[1]))
        assert claims["role"] == 1 and claims["iat"] == 70 and claims["exp"] == 1870 and claims["tokenExp"] == 1870


class FakeZoom:
    """Zoom's token endpoint and the user token endpoint, with scripted answers."""

    def __init__(self, token_status=200, zak_status=200, zak_body=None):
        self.token_status, self.zak_status = token_status, zak_status
        self.zak_body = zak_body if zak_body is not None else {"token": "ZAK-abc"}
        self.token_calls, self.zak_calls = [], []

    def app(self):
        app = web.Application()
        app.router.add_post("/oauth/token", self.token)
        app.router.add_get("/users/{user}/token", self.zak)
        return app

    async def token(self, request):
        self.token_calls.append({"query": dict(request.query), "auth": request.headers.get("Authorization", "")})
        if self.token_status != 200:
            return web.json_response({"reason": "Invalid client_id or client_secret", "error": "invalid_client"}, status=self.token_status)
        return web.json_response({"access_token": "ACCESS-1", "token_type": "bearer", "expires_in": 3599})

    async def zak(self, request):
        self.zak_calls.append({"user": request.match_info["user"], "query": dict(request.query), "auth": request.headers.get("Authorization", "")})
        return web.json_response(self.zak_body, status=self.zak_status)


async def api_for(fake):
    server = TestServer(fake.app())
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    api = ZoomApi("acct789", "s2sClient", "s2sSecret", oauth_url=base + "/oauth/token", api_url=base)
    return server, api


class TestZoomApi:
    async def test_fetches_a_zak_with_the_right_requests(self):
        fake = FakeZoom()
        server, api = await api_for(fake)
        try:
            assert await api.user_zak("room1@crystalpm.com") == "ZAK-abc"
        finally:
            await server.close()
        [token_call] = fake.token_calls
        assert token_call["query"] == {"grant_type": "account_credentials", "account_id": "acct789"}
        assert token_call["auth"] == "Basic " + base64.b64encode(b"s2sClient:s2sSecret").decode()
        [zak_call] = fake.zak_calls
        assert zak_call["user"] == "room1@crystalpm.com"
        assert zak_call["query"] == {"type": "zak", "ttl": "7200"}
        assert zak_call["auth"] == "Bearer ACCESS-1"

    async def test_caches_the_access_token(self):
        fake = FakeZoom()
        server, api = await api_for(fake)
        try:
            await api.user_zak("room1@crystalpm.com")
            await api.user_zak("room1@crystalpm.com")
        finally:
            await server.close()
        assert len(fake.token_calls) == 1 and len(fake.zak_calls) == 2


class TestZoomApiErrors:
    async def test_refused_credentials(self):
        server, api = await api_for(FakeZoom(token_status=401))
        try:
            with pytest.raises(ZoomAuthError) as failure:
                await api.access_token()
        finally:
            await server.close()
        assert "Zoom refused the server-to-server credentials" in str(failure.value)
        assert "activated" in str(failure.value)

    async def test_unknown_user(self):
        server, api = await api_for(FakeZoom(zak_status=404, zak_body={"code": 1001, "message": "User does not exist: nobody@crystalpm.com"}))
        try:
            with pytest.raises(ZoomAuthError) as failure:
                await api.user_zak("nobody@crystalpm.com")
        finally:
            await server.close()
        assert str(failure.value) == "Zoom has no user nobody@crystalpm.com on this account"

    async def test_missing_scope(self):
        server, api = await api_for(FakeZoom(zak_status=400, zak_body={"code": 4711, "message": "Invalid access token, does not contain scopes:[user:read:token:admin]"}))
        try:
            with pytest.raises(ZoomAuthError) as failure:
                await api.user_zak("room1@crystalpm.com")
        finally:
            await server.close()
        assert "lacks the user token scope (user:read:token:admin)" in str(failure.value)

    async def test_other_refusal_quotes_zoom(self):
        server, api = await api_for(FakeZoom(zak_status=429, zak_body={"message": "Too many requests"}))
        try:
            with pytest.raises(ZoomAuthError) as failure:
                await api.user_zak("room1@crystalpm.com")
        finally:
            await server.close()
        assert "429" in str(failure.value) and "Too many requests" in str(failure.value)

    async def test_unreachable(self):
        api = ZoomApi("acct", "id", "secret", oauth_url="http://127.0.0.1:9/oauth/token", api_url="http://127.0.0.1:9")
        with pytest.raises(ZoomAuthError) as failure:
            await api.access_token()
        assert str(failure.value).startswith("could not reach Zoom")
