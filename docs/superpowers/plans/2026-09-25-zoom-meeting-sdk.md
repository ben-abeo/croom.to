# Zoom Meeting SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Crystal Meet rooms join Zoom meetings, including ones hosted by outside accounts, through Zoom's Meeting SDK instead of the public web client that blocks automated guests.

**Architecture:** A new `zoom` provider serves one page on the device's loopback address, opens it in the room's headed Chromium, and lets Zoom's web SDK (Client View 6.5.0) render the meeting on the TV. The device mints the SDK signature and, when a room Zoom user is configured, fetches that user's ZAK through a Server-to-Server OAuth credential; both travel to the page through a one-time join token that never appears in a URL. A JavaScript bridge reports the SDK's status events back to Python and exposes mute and leave; the camera toggle presses the SDK's toolbar button. Credentials live in one file installed by the installer; a check command explains the setup in plain words; a third brand guide splits the Zoom admin's part from the device part.

**Tech Stack:** Python 3.12 with aiohttp (loopback site and Zoom API calls) and Playwright (already present), standard-library HMAC for the JWT, Zoom Meeting SDK for Web 6.5.0 from `source.zoom.us`, pytest with `asyncio_mode = "auto"`, the venv's headless Chromium for page and provider tests, bash installer, the shared guide renderer.

**Spec:** `docs/superpowers/specs/2026-09-25-zoom-meeting-sdk-design.md`

## Global Constraints

- Internal names stay: the `croom` package and command, systemd units, `/etc/croom`. Only what a person reads says Crystal Meet.
- No secrets in the repo: `deploy/rooms/` and `docs/` carry placeholders only. On a device the credentials file is `/etc/croom/zoom-credentials.json`, owned by the service user, mode 600. Values still starting with `REPLACE` count as placeholders.
- Credentials file keys exactly: `sdk_client_id`, `sdk_client_secret` (required); `account_id`, `s2s_client_id`, `s2s_client_secret`, `room_user` (all four or none).
- The SDK signature: header `{"alg": "HS256", "typ": "JWT"}`; payload `appKey`, `sdkKey` (both the client id), `mn` (digits as a string), `role`, `iat`, `exp`, `tokenExp` (`exp` and `tokenExp` equal `iat + ttl`, default 7200 s, Zoom's allowed range 1800 to 172800 s); HMAC-SHA256 with the client secret; base64url without padding.
- Zoom endpoints: token `POST https://zoom.us/oauth/token` with `grant_type=account_credentials` and `account_id`, HTTP Basic `client_id:client_secret`; ZAK `GET https://api.zoom.us/v2/users/{user}/token?type=zak&ttl=<seconds>` with the bearer token.
- The SDK page is served only on `127.0.0.1` with an ephemeral port; every response carries `Cross-Origin-Opener-Policy: same-origin` and `Cross-Origin-Embedder-Policy: credentialless`; any other peer gets 403; join parameters are served once.
- SDK version `6.5.0` loaded from `https://source.zoom.us/6.5.0/`; the page reports an error when `window.ZoomMtg` is missing after load.
- The room page, its API and the door sign are unchanged. The `zoom` platform name and `ZoomProvider`'s URL patterns stay the contract for links.
- No new Python dependencies. The page files ship as package data.
- Brand for the new guide exactly as the existing guides (tokens navy-900 `#001636`, navy-800 `#16244F`, blue-600 `#1B52E5`, blue-700 `#003EBC`, periwinkle-300 `#BDCEFF`, tint-100 `#EDF2FE`, tint-50 `#F7F9FF`, slate-500 `#647087`, ink-900 `#1C2024`; Lexend; kicker labels the only uppercase text; sentence-case headlines; self-contained assets).
- Work on branch `zoom-sdk` (created from `main` at 06f09d0; the spec commit cbf8cd6 is on it). Commit after each task with a `type(scope): summary` message and no attribution trailers.
- Run tests with `.venv/bin/pytest` from the repo root. After every task run `bash /tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/suite-gate.sh <label>` and confirm `GATE: PASSED` (the upstream baseline is 81 failing tests since the Google libraries were installed; the gate compares failing ids, so 81 or fewer with no new ids passes) and every test this plan adds passing.
- Browser tests use the venv's headless Chromium with `https://source.zoom.us/**` blocked and a stub `window.ZoomMtg` injected; nothing in the tests reaches Zoom.

## Review Focus

1. A link for a passcode-protected meeting pasted without its passcode: Zoom's SDK error must reach the room page in words, not a silent hang. Pinned by Task 3 `test_join_error_reaches_the_room_page_in_zooms_words` and Task 2 `test_join_error_is_reported_in_words`.
2. The ZAK fetch failing (wrong scope, unknown user, credentials refused): nothing opens on the TV and the error names what to fix. Pinned by Task 3 `test_zak_failure_reports_and_opens_nothing` and Task 1 `TestZoomApiErrors`.
3. A device without internet, or Zoom's CDN unreachable: a clear error rather than a blank TV. Pinned by Task 2 `test_missing_sdk_is_reported`.
4. Two joins in a row: join parameters are served once, and the second join after leaving works. Pinned by Task 2 `test_join_parameters_are_served_once` and Task 3 `test_join_leave_join_again`.
5. Another machine on the network reaching the loopback site: refused. Pinned by Task 2 `test_other_peers_are_refused`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/croom/core/config.py` | `MeetingConfig.zoom_credentials_path`. |
| `src/croom/meeting/zoom_auth.py` | Credentials file, not-configured rule, `meeting_sdk_signature`, `ZoomApi`, `ZoomAuthError`. |
| `src/croom/meeting/providers/zoom_sdk_site.py` | The loopback site: page, script, one-time join parameters, headers, loopback check. |
| `src/croom/meeting/providers/zoom_sdk_page/meeting.html`, `meeting.js` | The SDK page and the `crystalMeet` bridge. |
| `src/croom/meeting/providers/zoom_sdk.py` | `ZoomSdkProvider`: browser lifecycle, join flow, controls. |
| `src/croom/meeting/providers/__init__.py`, `src/croom/meeting/service.py` | `build_provider(platform, config)` and the one warning. |
| `src/croom/meeting/zoom_check.py`, `src/croom/core/agent.py` | `croom --check-zoom`. |
| `pyproject.toml` | Package data for the page files. |
| `installer/install.sh`, `deploy/rooms/*.yaml`, `deploy/rooms/README.md` | `--zoom-credentials`, config key, docs. |
| `docs/guides/crystal-meet-zoom/*`, `docs/guides/crystal-meet-zoom.pdf`, `README.md` | The guide and README updates. |
| `tests/unit/meeting/zoom_stub.py` | The stub `ZoomMtg` and CDN block shared by page and provider tests. |
| `tests/unit/meeting/test_zoom_auth.py`, `test_zoom_sdk_site.py`, `test_zoom_sdk_page.py`, `test_zoom_sdk_provider.py`, `test_zoom_selection.py`, `test_zoom_check.py` | Tests. |
| `tests/unit/installer/test_install_script.py`, `tests/unit/deploy/test_room_configs.py`, `tests/unit/docs/test_zoom_guide.py`, `tests/unit/docs/test_readme.py` | Installer, configs, guide and README tests. |

---

### Task 1: Credentials, the not-configured rule, the signature and the Zoom API client

**Files:**
- Modify: `src/croom/core/config.py` (MeetingConfig, `to_dict` meeting section)
- Create: `src/croom/meeting/zoom_auth.py`
- Test: `tests/unit/meeting/test_zoom_auth.py`

**Interfaces:**
- Consumes: `Config` (existing).
- Produces: `MeetingConfig.zoom_credentials_path: str`; in `croom.meeting.zoom_auth`: `PLACEHOLDER = "REPLACE"`, `class ZoomAuthError(Exception)`, `@dataclass ZoomCredentials(sdk_client_id, sdk_client_secret, account_id="", s2s_client_id="", s2s_client_secret="", room_user="")` with property `has_room_user`, `zoom_not_configured_reason(path: str) -> Optional[str]`, `load_zoom_credentials(path: str) -> ZoomCredentials` (raises `ZoomAuthError` with the reason), `meeting_sdk_signature(client_id, client_secret, meeting_number, role=0, now=None, ttl_seconds=7200) -> str`, `class ZoomApi(account_id, client_id, client_secret, oauth_url=..., api_url=...)` with `async access_token() -> str` and `async user_zak(user, ttl_seconds=7200) -> str`. Tasks 3, 4 and 5 use exactly these.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/meeting/test_zoom_auth.py`:

```python
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
        assert claims == {
            "appKey": "sdkClient123", "sdkKey": "sdkClient123", "mn": "99612060433", "role": 0,
            "iat": 1_800_000_000, "exp": 1_800_007_200, "tokenExp": 1_800_007_200,
        }
        assert "=" not in token

    def test_custom_role_and_ttl(self):
        claims = json.loads(b64url_decode(meeting_sdk_signature("a", "b", "123", role=1, now=100, ttl_seconds=1800).split(".")[1]))
        assert claims["role"] == 1 and claims["exp"] == 1900 and claims["tokenExp"] == 1900


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/meeting/test_zoom_auth.py -q -p no:cacheprovider`
Expected: FAIL: `ModuleNotFoundError: No module named 'croom.meeting.zoom_auth'`.

- [ ] **Step 3: Add the config field**

In `src/croom/core/config.py`, in `class MeetingConfig`, after `mic_default_on: bool = True` add:

```python
    zoom_credentials_path: str = ""  # /etc/croom/zoom-credentials.json on a room device
```

In `to_dict`, in the `"meeting"` section, after `"mic_default_on": self.meeting.mic_default_on,` add:

```python
                "zoom_credentials_path": self.meeting.zoom_credentials_path,
```

- [ ] **Step 4: Write the auth module**

Create `src/croom/meeting/zoom_auth.py`:

```python
"""
Zoom credentials and tokens for the Meeting SDK provider (spec 2026-09-25 Zoom,
sections 4.2 and 4.3): the credentials file, the not-configured rule, the SDK
signature, and the Server-to-Server OAuth client that fetches a room user's ZAK.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import aiohttp

PLACEHOLDER = "REPLACE"
SDK_KEYS = ("sdk_client_id", "sdk_client_secret")
S2S_KEYS = ("account_id", "s2s_client_id", "s2s_client_secret", "room_user")
ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"
ZOOM_API_URL = "https://api.zoom.us/v2"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class ZoomAuthError(Exception):
    """A credential or token problem, worded for the person setting up the room."""


@dataclass
class ZoomCredentials:
    sdk_client_id: str
    sdk_client_secret: str
    account_id: str = ""
    s2s_client_id: str = ""
    s2s_client_secret: str = ""
    room_user: str = ""

    @property
    def has_room_user(self) -> bool:
        """True when the room can join as its own Zoom user (all four server-to-server values present)."""
        return all((self.account_id, self.s2s_client_id, self.s2s_client_secret, self.room_user))


def _read_json_object(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _blank(data: Dict[str, Any], key: str) -> bool:
    value = data.get(key)
    return not isinstance(value, str) or not value.strip() or value.strip().startswith(PLACEHOLDER)


def zoom_not_configured_reason(path: str) -> Optional[str]:
    """Why the Zoom Meeting SDK cannot be used yet, in words for the person setting up the room; None when it can."""
    if not path:
        return "no zoom_credentials_path in the config"
    if not (os.path.isfile(path) and os.access(path, os.R_OK)):
        return f"credentials file not found or unreadable: {path}"
    data = _read_json_object(path)
    if data is None:
        return f"credentials file is not a JSON object: {path}"
    for key in SDK_KEYS:
        if _blank(data, key):
            return f"{key} is missing or still {PLACEHOLDER} in {path}"
    present = [key for key in S2S_KEYS if not _blank(data, key)]
    if present and len(present) != len(S2S_KEYS):
        missing = ", ".join(key for key in S2S_KEYS if key not in present)
        return f"{missing} missing or still {PLACEHOLDER} in {path}; the server-to-server values and room_user go together"
    return None


def load_zoom_credentials(path: str) -> ZoomCredentials:
    """The credentials file as a dataclass; raises ZoomAuthError with the not-configured reason."""
    reason = zoom_not_configured_reason(path)
    if reason:
        raise ZoomAuthError(reason)
    data = _read_json_object(path) or {}
    values = {key: str(data.get(key) or "").strip() for key in SDK_KEYS + S2S_KEYS}
    return ZoomCredentials(**values)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def meeting_sdk_signature(client_id: str, client_secret: str, meeting_number, role: int = 0,
                          now: Optional[float] = None, ttl_seconds: int = 7200) -> str:
    """A Meeting SDK JWT for one meeting: HS256 with the app's client secret (spec 4.3)."""
    issued = int(now if now is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "appKey": client_id,
        "sdkKey": client_id,
        "mn": str(meeting_number),
        "role": role,
        "iat": issued,
        "exp": issued + ttl_seconds,
        "tokenExp": issued + ttl_seconds,
    }
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "." + _b64url(json.dumps(payload, separators=(",", ":")).encode()))
    digest = hmac.new(client_secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64url(digest)


class ZoomApi:
    """Server-to-Server OAuth: an account access token, and a room user's ZAK."""

    def __init__(self, account_id: str, client_id: str, client_secret: str,
                 oauth_url: str = ZOOM_OAUTH_URL, api_url: str = ZOOM_API_URL):
        self._account_id = account_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._oauth_url = oauth_url
        self._api_url = api_url.rstrip("/")
        self._token: Optional[str] = None
        self._token_expires = 0.0

    async def access_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.post(
                    self._oauth_url,
                    params={"grant_type": "account_credentials", "account_id": self._account_id},
                    auth=aiohttp.BasicAuth(self._client_id, self._client_secret),
                ) as response:
                    status, body = response.status, await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise ZoomAuthError(f"could not reach Zoom to get a token: {e}") from e
        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            raise ZoomAuthError("Zoom refused the server-to-server credentials: check the account id, "
                                "client id and client secret, and that the app is activated")
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 3600))
        return self._token

    async def user_zak(self, user: str, ttl_seconds: int = 7200) -> str:
        token = await self.access_token()
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.get(
                    f"{self._api_url}/users/{user}/token",
                    params={"type": "zak", "ttl": str(ttl_seconds)},
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    status, body = response.status, await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise ZoomAuthError(f"could not reach Zoom to get the room user's ZAK: {e}") from e
        if status == 200 and isinstance(body, dict) and body.get("token"):
            return body["token"]
        message = str(body.get("message", "")) if isinstance(body, dict) else ""
        if status == 404:
            raise ZoomAuthError(f"Zoom has no user {user} on this account")
        if status in (400, 401, 403) and "scope" in message.lower():
            raise ZoomAuthError("the server-to-server app lacks the user token scope (user:read:token:admin); "
                                "add it and re-activate the app")
        raise ZoomAuthError(f"Zoom refused the ZAK for {user} ({status}): {message or 'no details'}")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/meeting/test_zoom_auth.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 7: Commit**

```bash
git add src/croom/core/config.py src/croom/meeting/zoom_auth.py tests/unit/meeting/test_zoom_auth.py
git commit -m "feat(meeting): Zoom credentials file, Meeting SDK signature and server-to-server ZAK client"
```

---

### Task 2: The loopback site and the SDK page

**Files:**
- Create: `src/croom/meeting/providers/zoom_sdk_site.py`, `src/croom/meeting/providers/zoom_sdk_page/meeting.html`, `src/croom/meeting/providers/zoom_sdk_page/meeting.js`, `tests/unit/meeting/zoom_stub.py`
- Modify: `pyproject.toml` (package data)
- Test: `tests/unit/meeting/test_zoom_sdk_site.py`, `tests/unit/meeting/test_zoom_sdk_page.py`

**Interfaces:**
- Consumes: nothing from Task 1 (the site is token-agnostic).
- Produces: `croom.meeting.providers.zoom_sdk_site.ZoomSdkSite(host="127.0.0.1", port=0)` with `register_join(params: dict) -> str`, `create_app() -> aiohttp.web.Application`, `async start() -> int` (the bound port, also in `.port`), `async stop()`, `url(path="/meeting") -> str`; page bridge `window.crystalMeet` with `state` (`loading`, `joining`, `waiting`, `connected`, `reconnecting`, `left`, `error`), `detail`, `userId`, `muted`, `mute(muted) -> Promise<boolean>`, `leave() -> Promise<boolean>`, and the page calling `window.crystalMeetEvent(state, detail)` on every change; `tests.unit.meeting.zoom_stub.STUB_JS` and `block_sdk_cdn(context)`. Task 3 relies on all of it.

- [ ] **Step 1: Write the shared stub and the failing tests**

Create `tests/unit/meeting/zoom_stub.py`:

```python
"""
A stand-in for Zoom's Meeting SDK (window.ZoomMtg) so the page and the provider
can be driven in the venv's Chromium without reaching Zoom. Behaviour flags:
window.ZoomMtg._behaviour = {waiting, errorFor, neverConnect, delayMs}.
"""

STUB_JS = r"""
window.__zoomCalls = [];
window.ZoomMtg = {
  _listeners: {},
  _behaviour: { waiting: false, errorFor: "999", neverConnect: false, delayMs: 50 },
  setZoomJSLib(path, av) { window.__zoomCalls.push(["setZoomJSLib", path, av]); },
  preLoadWasm() { window.__zoomCalls.push(["preLoadWasm"]); },
  prepareWebSDK() { window.__zoomCalls.push(["prepareWebSDK"]); },
  inMeetingServiceListener(name, cb) { this._listeners[name] = cb; },
  _fire(name, data) { if (this._listeners[name]) this._listeners[name](data); },
  init(opts) {
    const copy = Object.assign({}, opts); delete copy.success; delete copy.error;
    window.__zoomCalls.push(["init", copy]);
    setTimeout(() => opts.success && opts.success(), 10);
  },
  join(opts) {
    const copy = Object.assign({}, opts); delete copy.success; delete copy.error;
    window.__zoomCalls.push(["join", copy]);
    const b = this._behaviour;
    if (String(opts.meetingNumber) === b.errorFor) {
      setTimeout(() => opts.error && opts.error({ errorCode: 3712, errorMessage: "This meeting ID is not valid" }), 10);
      return;
    }
    if (b.neverConnect) return;
    setTimeout(() => this._fire("onMeetingStatus", { meetingStatus: 1 }), 10);
    if (b.waiting) setTimeout(() => this._fire("onUserIsInWaitingRoom", {}), 20);
    setTimeout(() => {
      const button = document.createElement("button");
      button.id = "stub-video";
      button.setAttribute("aria-label", "Stop Video");
      button.addEventListener("click", () => button.setAttribute("aria-label",
        button.getAttribute("aria-label") === "Stop Video" ? "Start Video" : "Stop Video"));
      document.body.appendChild(button);
      this._fire("onMeetingStatus", { meetingStatus: 2 });
      opts.success && opts.success();
    }, b.waiting ? 300 : b.delayMs);
  },
  getCurrentUser(opts) { opts.success && opts.success({ result: { currentUser: { userId: 16777216, userName: "Room 1", muted: false } } }); },
  mute(opts) { window.__zoomCalls.push(["mute", { userId: opts.userId, mute: opts.mute }]); opts.success && opts.success(); },
  leaveMeeting(opts) { window.__zoomCalls.push(["leaveMeeting", { confirm: opts.confirm }]); opts.success && opts.success(); },
};
"""


async def block_sdk_cdn(context):
    """Abort every request to Zoom's CDN so the page runs on the stub only."""

    async def abort(route):
        await route.abort()

    await context.route("https://source.zoom.us/**", abort)
```

Create `tests/unit/meeting/test_zoom_sdk_site.py`:

```python
"""
The loopback site that hosts the Zoom SDK page (spec 2026-09-25 Zoom, section
4.4): page and script served, one-time join parameters, cross-origin headers,
loopback only.
"""

from unittest import mock

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from croom.meeting.providers.zoom_sdk_site import CROSS_ORIGIN_HEADERS, ZoomSdkSite


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
```

Create `tests/unit/meeting/test_zoom_sdk_page.py`:

```python
"""
The SDK page and its crystalMeet bridge, driven in the venv's Chromium against
the stub ZoomMtg with Zoom's CDN blocked (spec 2026-09-25 Zoom, section 4.5).
"""

import asyncio

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.providers.zoom_sdk_site import ZoomSdkSite  # noqa: E402
from tests.unit.meeting.zoom_stub import STUB_JS, block_sdk_cdn  # noqa: E402

JOIN = {"meetingNumber": "99612060433", "passWord": "abc123", "userName": "Room 1", "signature": "sig.nature.x",
        "zak": "ZAK-1", "micOn": True, "cameraOn": True, "sdkVersion": "6.5.0"}


class PageRun:
    """A started site plus a browser page with the stub and an event recorder."""

    def __init__(self, params=None, stub=STUB_JS):
        self.params = dict(JOIN, **(params or {}))
        self.stub = stub
        self.events = []

    async def __aenter__(self):
        self.site = ZoomSdkSite()
        await self.site.start()
        self._pw = await playwright.async_playwright().start()
        self.browser = await self._pw.chromium.launch()
        self.context = await self.browser.new_context()
        await block_sdk_cdn(self.context)
        if self.stub:
            await self.context.add_init_script(self.stub)
        self.page = await self.context.new_page()
        await self.page.expose_function("crystalMeetEvent", lambda state, detail: self.events.append((state, detail)))
        token = self.site.register_join(self.params)
        await self.page.goto(self.site.url() + "#" + token, wait_until="load")
        return self

    async def __aexit__(self, *exc):
        await self.browser.close()
        await self._pw.stop()
        await self.site.stop()

    async def wait_for(self, state, timeout=5.0):
        for _ in range(int(timeout * 20)):
            if any(s == state for s, _ in self.events):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"state {state!r} never reported; events: {self.events}")

    async def calls(self, name):
        return [c for c in await self.page.evaluate("window.__zoomCalls") if c[0] == name]


async def test_joins_with_the_one_time_parameters_and_reports_connected():
    async with PageRun() as run:
        await run.wait_for("connected")
        [init] = await run.calls("init")
        assert init[1]["leaveUrl"] == "/left" and init[1]["disablePreview"] is True and init[1]["showMeetingHeader"] is False
        [join] = await run.calls("join")
        assert join[1] == {"signature": "sig.nature.x", "meetingNumber": "99612060433", "passWord": "abc123",
                           "userName": "Room 1", "userEmail": "", "zak": "ZAK-1"}
        assert run.events[0][0] == "joining" and run.events[-1][0] == "connected"
        assert await run.page.evaluate("window.crystalMeet.state") == "connected"
        assert await run.page.evaluate("window.crystalMeet.userId") == 16777216
        [lib] = await run.calls("setZoomJSLib")
        assert lib[1:] == ["https://source.zoom.us/6.5.0/lib", "/av"]


async def test_join_without_zak_omits_it():
    async with PageRun({"zak": None}) as run:
        await run.wait_for("connected")
        [join] = await run.calls("join")
        assert "zak" not in join[1]


async def test_join_error_is_reported_in_words():
    async with PageRun({"meetingNumber": "999"}) as run:
        await run.wait_for("error")
        state, detail = [e for e in run.events if e[0] == "error"][0]
        assert detail == "This meeting ID is not valid (code 3712)"


async def test_waiting_room_is_reported_before_connected():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.waiting = true;") as run:
        await run.wait_for("connected")
        states = [s for s, _ in run.events]
        assert "waiting" in states and states.index("waiting") < states.index("connected")


async def test_mute_and_leave_go_through_the_sdk():
    async with PageRun() as run:
        await run.wait_for("connected")
        assert await run.page.evaluate("window.crystalMeet.mute(true)") is True
        assert await run.page.evaluate("window.crystalMeet.mute(false)") is False
        assert [c[1] for c in await run.calls("mute")] == [{"userId": 16777216, "mute": True}, {"userId": 16777216, "mute": False}]
        assert await run.page.evaluate("window.crystalMeet.leave()") is True
        assert [c[1] for c in await run.calls("leaveMeeting")] == [{"confirm": False}]
        await run.wait_for("left")


async def test_mic_off_at_join_mutes_after_connecting():
    async with PageRun({"micOn": False}) as run:
        await run.wait_for("connected")
        for _ in range(40):
            if await run.calls("mute"):
                break
            await asyncio.sleep(0.05)
        assert [c[1] for c in await run.calls("mute")] == [{"userId": 16777216, "mute": True}]


async def test_missing_sdk_is_reported():
    async with PageRun(stub="") as run:
        await run.wait_for("error")
        detail = [d for s, d in run.events if s == "error"][0]
        assert "did not load from source.zoom.us" in detail


async def test_used_join_token_is_reported():
    async with PageRun() as run:
        await run.wait_for("connected")
        token = run.site.register_join(JOIN)
        await run.page.goto(run.site.url() + "#" + token, wait_until="load")
        await run.wait_for("connected")
        run.events.clear()
        await run.page.goto(run.site.url() + "#" + token, wait_until="load")  # the same token again
        await run.wait_for("error")
        assert "join parameters expired" in [d for s, d in run.events if s == "error"][0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/meeting/test_zoom_sdk_site.py tests/unit/meeting/test_zoom_sdk_page.py -q -p no:cacheprovider`
Expected: FAIL: `ModuleNotFoundError: No module named 'croom.meeting.providers.zoom_sdk_site'`.

- [ ] **Step 3: Write the site**

Create `src/croom/meeting/providers/zoom_sdk_site.py`:

```python
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
```

- [ ] **Step 4: Write the page and its bridge**

Create `src/croom/meeting/providers/zoom_sdk_page/meeting.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crystal Meet</title>
<style>
html, body { margin: 0; height: 100%; background: #111827; color: #fff; font-family: Lexend, "Segoe UI", Arial, sans-serif; }
#crystal-meet-status { position: fixed; top: 12px; left: 12px; margin: 0; font-size: 14px; opacity: 0.8; z-index: 1; }
</style>
</head>
<body>
<p id="crystal-meet-status">Connecting to Zoom</p>
<script src="https://source.zoom.us/6.5.0/lib/vendor/react.min.js"></script>
<script src="https://source.zoom.us/6.5.0/lib/vendor/react-dom.min.js"></script>
<script src="https://source.zoom.us/6.5.0/lib/vendor/react-redux.min.js"></script>
<script src="https://source.zoom.us/6.5.0/lib/vendor/redux.min.js"></script>
<script src="https://source.zoom.us/6.5.0/lib/vendor/redux-thunk.min.js"></script>
<script src="https://source.zoom.us/6.5.0/lib/vendor/lodash.min.js"></script>
<script src="https://source.zoom.us/6.5.0/zoom-meeting-6.5.0.min.js"></script>
<script src="/static/meeting.js"></script>
</body>
</html>
```

Create `src/croom/meeting/providers/zoom_sdk_page/meeting.js`:

```javascript
// The bridge between the room device and Zoom's Meeting SDK (Client View).
// Reads one-time join parameters, joins, and reports every status change to the
// device through window.crystalMeetEvent (exposed by the provider).
(function () {
  "use strict";

  const SDK_VERSION = "6.5.0";
  const STATUS = { 1: "joining", 2: "connected", 3: "left", 4: "reconnecting" };
  const bridge = { state: "loading", detail: "", userId: null, muted: false };
  window.crystalMeet = bridge;

  function report(state, detail) {
    bridge.state = state;
    bridge.detail = detail || "";
    const label = document.getElementById("crystal-meet-status");
    if (label) label.textContent = state === "connected" ? "" : (bridge.detail || state);
    if (typeof window.crystalMeetEvent === "function") {
      try { window.crystalMeetEvent(state, bridge.detail); } catch (e) { /* the device is not listening */ }
    }
  }

  function words(error) {
    if (!error) return "Zoom gave no reason";
    const text = error.errorMessage || error.reason || error.message || "";
    const code = error.errorCode !== undefined && error.errorCode !== null ? " (code " + error.errorCode + ")" : "";
    return (text || JSON.stringify(error)) + code;
  }

  bridge.mute = function (muted) {
    return new Promise((resolve, reject) => {
      if (bridge.userId === null) { reject(new Error("not in a meeting")); return; }
      ZoomMtg.mute({
        userId: bridge.userId, mute: !!muted,
        success: () => { bridge.muted = !!muted; resolve(bridge.muted); },
        error: (e) => reject(new Error(words(e))),
      });
    });
  };

  bridge.leave = function () {
    return new Promise((resolve) => {
      ZoomMtg.leaveMeeting({
        confirm: false,
        success: () => { report("left", ""); resolve(true); },
        error: () => { report("left", ""); resolve(false); },
      });
    });
  };

  function afterConnect(params) {
    ZoomMtg.getCurrentUser({
      success: (result) => {
        const user = result && result.result && result.result.currentUser;
        if (user) { bridge.userId = user.userId; bridge.muted = !!user.muted; }
        if (params.micOn === false && bridge.userId !== null) bridge.mute(true).catch(() => {});
      },
    });
  }

  async function main() {
    if (typeof window.ZoomMtg === "undefined") {
      report("error", "Zoom's SDK did not load from source.zoom.us; check the device's internet access");
      return;
    }
    const token = location.hash.replace(/^#/, "");
    let params;
    try {
      const response = await fetch("/join/" + encodeURIComponent(token));
      if (!response.ok) throw new Error("join parameters expired (" + response.status + ")");
      params = await response.json();
    } catch (e) {
      report("error", "Could not read the join parameters: " + e.message);
      return;
    }
    ZoomMtg.setZoomJSLib("https://source.zoom.us/" + (params.sdkVersion || SDK_VERSION) + "/lib", "/av");
    ZoomMtg.preLoadWasm();
    ZoomMtg.prepareWebSDK();
    ZoomMtg.inMeetingServiceListener("onMeetingStatus", (data) => {
      const state = STATUS[data && data.meetingStatus] || "joining";
      if (state === "connected") afterConnect(params);
      report(state, "");
    });
    ZoomMtg.inMeetingServiceListener("onUserIsInWaitingRoom", () => report("waiting", "Waiting for the host to let the room in"));
    report("joining", "");
    ZoomMtg.init({
      leaveUrl: "/left",
      disablePreview: true,
      disableInvite: true,
      disableRecord: true,
      showMeetingHeader: false,
      isSupportChat: false,
      leaveOnPageUnload: true,
      patchJsMedia: true,
      disableCORP: !window.crossOriginIsolated,
      success: () => {
        const join = {
          signature: params.signature,
          meetingNumber: String(params.meetingNumber),
          passWord: params.passWord || "",
          userName: params.userName,
          userEmail: "",
          success: () => {},
          error: (e) => report("error", words(e)),
        };
        if (params.zak) join.zak = params.zak;
        ZoomMtg.join(join);
      },
      error: (e) => report("error", "Zoom's SDK could not start: " + words(e)),
    });
  }

  main();
})();
```

In `pyproject.toml` replace

```toml
croom = ["py.typed", "control/static/*", "control/static/fonts/*"]
```

with

```toml
croom = ["py.typed", "control/static/*", "control/static/fonts/*", "meeting/providers/zoom_sdk_page/*"]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/meeting/test_zoom_sdk_site.py tests/unit/meeting/test_zoom_sdk_page.py -q -p no:cacheprovider`
Expected: all pass (the page tests take a few seconds each for the browser).

- [ ] **Step 6: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 7: Commit**

```bash
git add src/croom/meeting/providers/zoom_sdk_site.py src/croom/meeting/providers/zoom_sdk_page pyproject.toml tests/unit/meeting/zoom_stub.py tests/unit/meeting/test_zoom_sdk_site.py tests/unit/meeting/test_zoom_sdk_page.py
git commit -m "feat(meeting): loopback site and bridge page for Zoom's Meeting SDK"
```

---

### Task 3: The provider

**Files:**
- Create: `src/croom/meeting/providers/zoom_sdk.py`
- Test: `tests/unit/meeting/test_zoom_sdk_provider.py`

**Interfaces:**
- Consumes: `ZoomCredentials`, `ZoomApi`, `ZoomAuthError`, `load_zoom_credentials`, `meeting_sdk_signature` (Task 1); `ZoomSdkSite` and the page bridge events (Task 2); `ZoomProvider.can_handle_url` / `extract_meeting_id` (existing); `tests.unit.meeting.zoom_stub.STUB_JS`.
- Produces: `croom.meeting.providers.zoom_sdk.ZoomSdkProvider(credentials, room_name="Conference Room", api=None, headless=False, extra_init_script=None, block_sdk_cdn=False, site=None)` implementing `MeetingProvider` (`name == "zoom"`), with `classmethod from_config(config: Config) -> ZoomSdkProvider` and class attributes `SDK_VERSION`, `CONNECT_TIMEOUT_S`, `LOBBY_TIMEOUT_S`. Task 4 constructs it through `from_config`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/meeting/test_zoom_sdk_provider.py`:

```python
"""
The Meeting SDK provider end to end in the venv's headless Chromium against the
stub SDK (spec 2026-09-25 Zoom, section 4.4): joining, the waiting room, Zoom's
errors, the ZAK path, mute, camera, leave, and joining again.
"""

import json

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.core.config import Config  # noqa: E402
from croom.meeting.providers.base import MeetingState  # noqa: E402
from croom.meeting.providers.zoom_sdk import ZoomSdkProvider  # noqa: E402
from croom.meeting.zoom_auth import ZoomAuthError, ZoomCredentials  # noqa: E402
from tests.unit.meeting.zoom_stub import STUB_JS  # noqa: E402

LINK = "https://zoom.us/j/99612060433?pwd=abc123"
FULL = ZoomCredentials("sdkClient123", "sdkSecret456", "acct789", "s2sClient", "s2sSecret", "room1@crystalpm.com")
SDK_ONLY = ZoomCredentials("sdkClient123", "sdkSecret456")


class FakeApi:
    def __init__(self, zak="ZAK-1", error=None):
        self.zak, self.error, self.calls = zak, error, []

    async def user_zak(self, user, ttl_seconds=7200):
        self.calls.append(user)
        if self.error:
            raise ZoomAuthError(self.error)
        return self.zak


async def provider_for(credentials=FULL, api=None, stub=STUB_JS):
    provider = ZoomSdkProvider(credentials, room_name="Room 1", api=api, headless=True,
                               extra_init_script=stub, block_sdk_cdn=True)
    await provider.initialize()
    return provider


async def join_calls(provider):
    return [c[1] for c in await provider._page.evaluate("window.__zoomCalls") if c[0] == "join"]


class TestJoin:
    async def test_joins_as_the_room_user_and_connects(self):
        api = FakeApi()
        provider = await provider_for(api=api)
        try:
            states = []
            provider.add_state_callback(states.append)
            info = await provider.join_meeting(LINK, display_name="Room 1")
            assert provider.state == MeetingState.CONNECTED and info.meeting_id == "99612060433"
            assert states == [MeetingState.JOINING, MeetingState.CONNECTED]
            [join] = await join_calls(provider)
            assert join["meetingNumber"] == "99612060433" and join["passWord"] == "abc123"
            assert join["userName"] == "Room 1" and join["zak"] == "ZAK-1"
            assert join["signature"].count(".") == 2
            assert api.calls == ["room1@crystalpm.com"]
        finally:
            await provider.shutdown()

    async def test_without_a_room_user_joins_with_the_signature_only(self):
        provider = await provider_for(credentials=SDK_ONLY)
        try:
            await provider.join_meeting(LINK)
            [join] = await join_calls(provider)
            assert "zak" not in join
        finally:
            await provider.shutdown()

    async def test_zak_failure_reports_and_opens_nothing(self):
        words = "the server-to-server app lacks the user token scope (user:read:token:admin); add it and re-activate the app"
        provider = await provider_for(api=FakeApi(error=words))
        try:
            with pytest.raises(ZoomAuthError):
                await provider.join_meeting(LINK)
            assert provider.state == MeetingState.ERROR
            assert provider.current_meeting.error_message == words
            assert provider._page.url == "about:blank"
        finally:
            await provider.shutdown()

    async def test_join_error_reaches_the_room_page_in_zooms_words(self):
        provider = await provider_for(api=FakeApi())
        try:
            with pytest.raises(RuntimeError) as failure:
                await provider.join_meeting("https://zoom.us/j/999?pwd=x")
            assert "This meeting ID is not valid (code 3712)" in str(failure.value)
            assert provider.state == MeetingState.ERROR
            assert provider.current_meeting.error_message == str(failure.value)
        finally:
            await provider.shutdown()

    async def test_waiting_room_then_connected(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.waiting = true;")
        try:
            states = []
            provider.add_state_callback(states.append)
            await provider.join_meeting(LINK)
            assert states == [MeetingState.JOINING, MeetingState.IN_LOBBY, MeetingState.CONNECTED]
        finally:
            await provider.shutdown()

    async def test_never_connecting_times_out_with_the_pages_words(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.neverConnect = true;")
        provider.CONNECT_TIMEOUT_S = 1
        try:
            with pytest.raises(RuntimeError) as failure:
                await provider.join_meeting(LINK)
            assert "Zoom did not connect; the page says:" in str(failure.value)
            assert provider.state == MeetingState.ERROR
        finally:
            await provider.shutdown()


class TestControls:
    async def test_mute_camera_and_leave(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            assert await provider.toggle_mute() is True and provider.current_meeting.is_muted is True
            assert await provider.toggle_mute() is False and provider.current_meeting.is_muted is False
            assert await provider.toggle_camera() is False and provider.current_meeting.is_camera_on is False
            assert await provider._page.get_attribute("#stub-video", "aria-label") == "Start Video"
            assert await provider.toggle_camera() is True
            await provider.leave_meeting()
            assert provider.state == MeetingState.IDLE and provider.current_meeting is None
            assert provider._page.url == "about:blank"
        finally:
            await provider.shutdown()

    async def test_camera_off_at_join_presses_the_video_button(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK, camera_on=False)
            assert await provider._page.get_attribute("#stub-video", "aria-label") == "Start Video"
            assert provider.current_meeting.is_camera_on is False
        finally:
            await provider.shutdown()

    async def test_controls_need_a_meeting(self):
        provider = await provider_for(api=FakeApi())
        try:
            with pytest.raises(RuntimeError):
                await provider.toggle_mute()
            with pytest.raises(RuntimeError):
                await provider.toggle_camera()
        finally:
            await provider.shutdown()


class TestAgain:
    async def test_join_leave_join_again(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            await provider.leave_meeting()
            await provider.join_meeting(LINK)
            assert provider.state == MeetingState.CONNECTED
            [join] = await join_calls(provider)
            assert join["meetingNumber"] == "99612060433"
        finally:
            await provider.shutdown()


class TestUrlsAndConfig:
    def test_delegates_link_handling_to_the_zoom_patterns(self):
        assert ZoomSdkProvider.can_handle_url("https://us02web.zoom.us/j/123?pwd=x") is True
        assert ZoomSdkProvider.extract_meeting_id("https://zoom.us/wc/99612060433/join") == "99612060433"
        assert ZoomSdkProvider.can_handle_url("https://meet.google.com/abc-defg-hij") is False

    def test_from_config_reads_the_credentials_file(self, tmp_path):
        path = tmp_path / "zoom-credentials.json"
        path.write_text(json.dumps({"sdk_client_id": "a", "sdk_client_secret": "b"}))
        config = Config()
        config.meeting.zoom_credentials_path = str(path)
        config.room.name = "Room 2"
        provider = ZoomSdkProvider.from_config(config)
        assert provider.name == "zoom" and provider.display_name == "Zoom"
        assert provider._credentials.sdk_client_id == "a" and provider._room_name == "Room 2"
        assert provider._api is None

    def test_from_config_builds_the_api_for_a_room_user(self, tmp_path):
        path = tmp_path / "zoom-credentials.json"
        path.write_text(json.dumps({"sdk_client_id": "a", "sdk_client_secret": "b", "account_id": "c",
                                    "s2s_client_id": "d", "s2s_client_secret": "e", "room_user": "room2@crystalpm.com"}))
        config = Config()
        config.meeting.zoom_credentials_path = str(path)
        assert ZoomSdkProvider.from_config(config)._api is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/meeting/test_zoom_sdk_provider.py -q -p no:cacheprovider`
Expected: FAIL: `ModuleNotFoundError: No module named 'croom.meeting.providers.zoom_sdk'`.

- [ ] **Step 3: Write the provider**

Create `src/croom/meeting/providers/zoom_sdk.py`:

```python
"""
Zoom through the Meeting SDK (spec 2026-09-25 Zoom, section 4.4). The room's
headed Chromium opens a page served on this device's loopback address, and
Zoom's web SDK renders the meeting there. The device mints the signature and,
when a room Zoom user is configured, fetches that user's ZAK first, so meetings
hosted by other Zoom accounts can be joined too.
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from croom.core.config import Config
from croom.meeting.providers.base import MeetingInfo, MeetingProvider, MeetingState
from croom.meeting.providers.zoom import ZoomProvider
from croom.meeting.providers.zoom_sdk_site import ZoomSdkSite
from croom.meeting.zoom_auth import (
    ZoomApi,
    ZoomAuthError,
    ZoomCredentials,
    load_zoom_credentials,
    meeting_sdk_signature,
)

logger = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class ZoomSdkProvider(MeetingProvider):
    """Joins Zoom meetings with Zoom's Meeting SDK in the room's browser."""

    SDK_VERSION = "6.5.0"
    CONNECT_TIMEOUT_S = 90
    LOBBY_TIMEOUT_S = 300
    LEAVE_TIMEOUT_S = 5
    CLOSE_TIMEOUT_S = 5
    VIDEO_BUTTON = '[aria-label*="Start Video" i], [aria-label*="Stop Video" i]'
    BROWSER_ARGS = [
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-infobars",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1920,1080",
    ]
    USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def __init__(self, credentials: ZoomCredentials, room_name: str = "Conference Room",
                 api: Optional[ZoomApi] = None, headless: bool = False,
                 extra_init_script: Optional[str] = None, block_sdk_cdn: bool = False,
                 site: Optional[ZoomSdkSite] = None):
        super().__init__()
        self._credentials = credentials
        self._room_name = room_name
        if api is None and credentials.has_room_user:
            api = ZoomApi(credentials.account_id, credentials.s2s_client_id, credentials.s2s_client_secret)
        self._api = api
        self._headless = headless
        self._extra_init_script = extra_init_script
        self._block_sdk_cdn = block_sdk_cdn
        self._site = site or ZoomSdkSite()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._events: Optional[asyncio.Queue] = None
        self._muted = False
        self._camera_on = True

    @classmethod
    def from_config(cls, config: Config) -> "ZoomSdkProvider":
        credentials = load_zoom_credentials(config.meeting.zoom_credentials_path)
        return cls(credentials, room_name=config.room.name or "Conference Room")

    @property
    def name(self) -> str:
        return "zoom"

    @property
    def display_name(self) -> str:
        return "Zoom"

    @classmethod
    def can_handle_url(cls, url: str) -> bool:
        return ZoomProvider.can_handle_url(url)

    @classmethod
    def extract_meeting_id(cls, url: str) -> Optional[str]:
        return ZoomProvider.extract_meeting_id(url)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright not installed")
        self._events = asyncio.Queue()
        await self._site.start()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless, args=self.BROWSER_ARGS)
        self._context = await self._browser.new_context(
            permissions=["camera", "microphone"],
            viewport={"width": 1920, "height": 1080},
            user_agent=self.USER_AGENT,
        )
        if self._block_sdk_cdn:
            async def abort(route):
                await route.abort()
            await self._context.route("https://source.zoom.us/**", abort)
        if self._extra_init_script:
            await self._context.add_init_script(self._extra_init_script)
        self._page = await self._context.new_page()
        await self._page.expose_function("crystalMeetEvent", self._on_page_event)
        logger.info(f"Zoom Meeting SDK provider ready (page on http://127.0.0.1:{self._site.port}/meeting)")

    async def shutdown(self) -> None:
        if self._state == MeetingState.CONNECTED:
            await self.leave_meeting()
        for attribute in ("_page", "_context", "_browser"):
            closer = getattr(self, attribute)
            if closer is not None:
                try:
                    await asyncio.wait_for(closer.close(), timeout=self.CLOSE_TIMEOUT_S)
                except Exception as e:  # noqa: BLE001 - a hung browser must not hang the agent
                    logger.warning(f"Zoom browser {attribute[1:]} did not close cleanly: {e}")
                setattr(self, attribute, None)
        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=self.CLOSE_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Playwright did not stop cleanly: {e}")
            self._playwright = None
        await self._site.stop()

    def _on_page_event(self, state: str, detail: str = "") -> None:
        """Called by the page (through the exposed function) on every bridge state change."""
        if self._events is not None:
            self._events.put_nowait((str(state), str(detail or "")))

    def _drain_events(self) -> None:
        while self._events is not None and not self._events.empty():
            self._events.get_nowait()

    # ------------------------------------------------------------------
    # Joining
    # ------------------------------------------------------------------

    async def join_meeting(self, meeting_url: str, display_name: str = "Conference Room",
                           camera_on: bool = True, mic_on: bool = True) -> MeetingInfo:
        if self._page is None:
            raise RuntimeError("Provider not initialized")
        meeting_id = self.extract_meeting_id(meeting_url)
        if not meeting_id:
            raise ValueError(f"Invalid Zoom URL: {meeting_url}")
        passcode = parse_qs(urlparse(meeting_url).query).get("pwd", [""])[0]
        self._current_meeting = MeetingInfo(platform=self.name, meeting_id=meeting_id, meeting_url=meeting_url,
                                            is_camera_on=camera_on, is_muted=not mic_on)
        self._set_state(MeetingState.JOINING)
        logger.info(f"Joining Zoom meeting {meeting_id} through the Meeting SDK")
        try:
            zak = None
            if self._api is not None:
                zak = await self._api.user_zak(self._credentials.room_user)
            signature = meeting_sdk_signature(self._credentials.sdk_client_id, self._credentials.sdk_client_secret, meeting_id)
            params: Dict[str, Any] = {
                "meetingNumber": meeting_id, "passWord": passcode, "userName": display_name,
                "signature": signature, "zak": zak, "micOn": mic_on, "cameraOn": camera_on,
                "sdkVersion": self.SDK_VERSION,
            }
            token = self._site.register_join(params)
            self._drain_events()
            await self._page.goto(self._site.url("/meeting") + "#" + token, wait_until="load")
            await self._wait_for_connection()
            self._muted = not mic_on
            self._camera_on = True
            if not camera_on:
                await self._press_video_button()
                self._camera_on = False
            self._current_meeting.is_muted = self._muted
            self._current_meeting.is_camera_on = self._camera_on
            self._set_state(MeetingState.CONNECTED)
            logger.info(f"Connected to Zoom meeting {meeting_id}")
            return self._current_meeting
        except Exception as e:
            self._current_meeting.error_message = str(e)
            self._set_state(MeetingState.ERROR)
            logger.error(f"Zoom join failed: {e}")
            raise

    async def _wait_for_connection(self) -> None:
        """Follow the page's events until Zoom reports connected; a waiting room extends the wait."""
        deadline = time.monotonic() + self.CONNECT_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Zoom did not connect; the page says: " + await self._page_words())
            try:
                state, detail = await asyncio.wait_for(self._events.get(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if state == "connected":
                return
            if state == "waiting":
                if self._state != MeetingState.IN_LOBBY:
                    self._set_state(MeetingState.IN_LOBBY)
                    logger.info("Waiting in the Zoom waiting room")
                    deadline = time.monotonic() + self.LOBBY_TIMEOUT_S
            elif state == "error":
                raise RuntimeError(detail or "Zoom reported an error")
            elif state == "left":
                raise RuntimeError("Zoom ended the join before connecting")

    async def _page_words(self) -> str:
        try:
            return await self._page.evaluate("() => document.body.innerText.replace(/\\s+/g, ' ').trim().slice(0, 240)")
        except Exception:  # noqa: BLE001
            return "(page text unavailable)"

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------

    async def leave_meeting(self) -> None:
        if self._page is None:
            return
        self._set_state(MeetingState.LEAVING)
        try:
            await asyncio.wait_for(
                self._page.evaluate("() => (window.crystalMeet && window.crystalMeet.leave) ? window.crystalMeet.leave() : true"),
                timeout=self.LEAVE_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001 - the page may already be gone
            logger.warning(f"Zoom leave did not confirm: {e}")
        try:
            await self._page.goto("about:blank")
        except Exception:  # noqa: BLE001
            pass
        self._current_meeting = None
        self._set_state(MeetingState.IDLE)
        logger.info("Left the Zoom meeting")

    def _require_meeting(self) -> None:
        if self._state != MeetingState.CONNECTED or self._page is None:
            raise RuntimeError("Not in a meeting")

    async def toggle_mute(self) -> bool:
        self._require_meeting()
        muted = await self._page.evaluate("(muted) => window.crystalMeet.mute(muted)", not self._muted)
        self._muted = bool(muted)
        if self._current_meeting:
            self._current_meeting.is_muted = self._muted
        return self._muted

    async def toggle_camera(self) -> bool:
        self._require_meeting()
        await self._press_video_button()
        self._camera_on = not self._camera_on
        if self._current_meeting:
            self._current_meeting.is_camera_on = self._camera_on
        return self._camera_on

    async def _press_video_button(self) -> None:
        """The SDK has no own-video call; press the Client View's toolbar button."""
        button = await self._page.query_selector(self.VIDEO_BUTTON)
        if button is None:
            raise RuntimeError("Zoom's video button was not found")
        await button.click()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/meeting/test_zoom_sdk_provider.py -q -p no:cacheprovider`
Expected: all pass (each test launches a headless browser; about a minute in total).

- [ ] **Step 5: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 6: Commit**

```bash
git add src/croom/meeting/providers/zoom_sdk.py tests/unit/meeting/test_zoom_sdk_provider.py
git commit -m "feat(meeting): Zoom Meeting SDK provider joins through the loopback page with a signature and the room user's ZAK"
```

---

### Task 4: Choosing the provider

**Files:**
- Modify: `src/croom/meeting/providers/__init__.py`, `src/croom/meeting/service.py` (`start`)
- Test: `tests/unit/meeting/test_zoom_selection.py`

**Interfaces:**
- Consumes: `zoom_not_configured_reason` (Task 1), `ZoomSdkProvider.from_config` (Task 3).
- Produces: `croom.meeting.providers.build_provider(platform: str, config: Config) -> Optional[MeetingProvider]`; `MeetingService.start()` uses it. `get_provider` and `get_all_providers` stay as they are (upstream tests use them).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/meeting/test_zoom_selection.py`:

```python
"""
The zoom platform uses the Meeting SDK provider when its credentials are
configured, and the web client with exactly one warning otherwise (spec
2026-09-25 Zoom, section 4.2).
"""

import json
import logging
from unittest.mock import AsyncMock, patch

from croom.core.config import Config
from croom.meeting.providers import build_provider, get_provider
from croom.meeting.providers.google_meet import GoogleMeetProvider
from croom.meeting.providers.zoom import ZoomProvider
from croom.meeting.providers.zoom_sdk import ZoomSdkProvider
from croom.meeting.service import MeetingService


def config_with(tmp_path, credentials):
    config = Config()
    config.meeting.platforms = ["zoom", "google_meet"]
    if credentials is not None:
        path = tmp_path / "zoom-credentials.json"
        path.write_text(json.dumps(credentials))
        config.meeting.zoom_credentials_path = str(path)
    return config


def test_sdk_provider_when_credentials_are_configured(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        provider = build_provider("zoom", config_with(tmp_path, {"sdk_client_id": "a", "sdk_client_secret": "b"}))
    assert isinstance(provider, ZoomSdkProvider)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_web_client_with_one_warning_otherwise(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        provider = build_provider("zoom", config_with(tmp_path, None))
    assert isinstance(provider, ZoomProvider) and not isinstance(provider, ZoomSdkProvider)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["Zoom Meeting SDK not configured: no zoom_credentials_path in the config; "
                        "using the web client, which Zoom blocks for automated guests"]


def test_other_platforms_and_unknown_names(tmp_path):
    assert isinstance(build_provider("google_meet", config_with(tmp_path, None)), GoogleMeetProvider)
    assert build_provider("nope", config_with(tmp_path, None)) is None
    assert get_provider("zoom") is ZoomProvider  # the config-free registry is unchanged


async def test_meeting_service_starts_the_sdk_provider(tmp_path):
    config = config_with(tmp_path, {"sdk_client_id": "a", "sdk_client_secret": "b"})
    service = MeetingService(config)
    with patch.object(ZoomSdkProvider, "initialize", new=AsyncMock()), \
         patch.object(GoogleMeetProvider, "initialize", new=AsyncMock()):
        await service.start()
    assert isinstance(service._providers["zoom"], ZoomSdkProvider)
    assert service.get_available_platforms() == ["zoom", "google_meet"]


async def test_meeting_service_falls_back_to_the_web_client(tmp_path):
    service = MeetingService(config_with(tmp_path, None))
    with patch.object(ZoomProvider, "initialize", new=AsyncMock()), \
         patch.object(GoogleMeetProvider, "initialize", new=AsyncMock()):
        await service.start()
    assert type(service._providers["zoom"]) is ZoomProvider
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/meeting/test_zoom_selection.py -q -p no:cacheprovider`
Expected: FAIL: `ImportError: cannot import name 'build_provider'`.

- [ ] **Step 3: Add the builder and use it**

In `src/croom/meeting/providers/__init__.py` replace

```python
from croom.meeting.providers.base import MeetingProvider, MeetingInfo, MeetingState
```

with

```python
import logging

from croom.meeting.providers.base import MeetingProvider, MeetingInfo, MeetingState

logger = logging.getLogger(__name__)
```

and directly before `def get_all_providers() -> dict:` add:

```python
def build_provider(platform: str, config) -> "MeetingProvider | None":
    """
    The provider instance for a platform, given the agent's Config. Zoom uses
    the Meeting SDK when its credentials file is configured; otherwise the
    public web client, with one warning (spec 2026-09-25 Zoom, section 4.2).
    """
    if platform == "zoom":
        from croom.meeting.zoom_auth import zoom_not_configured_reason
        from croom.meeting.providers.zoom_sdk import ZoomSdkProvider

        reason = zoom_not_configured_reason(config.meeting.zoom_credentials_path)
        if reason is None:
            return ZoomSdkProvider.from_config(config)
        logger.warning(f"Zoom Meeting SDK not configured: {reason}; using the web client, "
                       "which Zoom blocks for automated guests")
        return ZoomProvider()
    provider_cls = get_provider(platform)
    return provider_cls() if provider_cls else None


```

and add `"build_provider",` to `__all__` after `"get_provider",`.

In `src/croom/meeting/service.py` replace

```python
from croom.meeting.providers import get_provider, get_all_providers
```

with

```python
from croom.meeting.providers import build_provider, get_all_providers
```

and in `start()` replace

```python
        for platform in self.config.meeting.platforms:
            provider_cls = get_provider(platform)
            if provider_cls:
                try:
                    provider = provider_cls()
                    await provider.initialize()
```

with

```python
        for platform in self.config.meeting.platforms:
            try:
                provider = build_provider(platform, self.config)
            except Exception as e:  # noqa: BLE001 - a bad credentials file must not stop the other platforms
                logger.error(f"Failed to build {platform} provider: {e}")
                continue
            if provider is not None:
                try:
                    await provider.initialize()
```

(the rest of the loop body, which stores the provider and logs, stays as it is). If `get_provider` is no longer referenced in `service.py`, do not add it back.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/meeting -q -p no:cacheprovider`
Expected: all pass, including the upstream `tests/unit/meeting/test_service.py`.

- [ ] **Step 5: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 6: Commit**

```bash
git add src/croom/meeting/providers/__init__.py src/croom/meeting/service.py tests/unit/meeting/test_zoom_selection.py
git commit -m "feat(meeting): use the Zoom Meeting SDK provider when its credentials are configured"
```

---

### Task 5: The check command

**Files:**
- Create: `src/croom/meeting/zoom_check.py`
- Modify: `src/croom/core/agent.py` (`main`)
- Test: `tests/unit/meeting/test_zoom_check.py`

**Interfaces:**
- Consumes: `zoom_not_configured_reason`, `load_zoom_credentials`, `meeting_sdk_signature`, `ZoomApi`, `ZoomAuthError` (Task 1); `load_config` (existing).
- Produces: `croom.meeting.zoom_check.check_zoom(config: Config, out: TextIO = sys.stdout, api_factory=None) -> int`; the `croom --check-zoom` flag. Tasks 6 and 7 quote the command.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/meeting/test_zoom_check.py`:

```python
"""
The Zoom check command's words and exit codes (spec 2026-09-25 Zoom, section
4.6), with a fake Zoom API, plus the command-line flag.
"""

import io
import json
import subprocess
import sys

import yaml

from croom.core.config import Config
from croom.meeting.zoom_auth import ZoomAuthError
from croom.meeting.zoom_check import check_zoom

FULL = {"sdk_client_id": "sdkClient123", "sdk_client_secret": "sdkSecret456", "account_id": "acct789",
        "s2s_client_id": "s2sClient", "s2s_client_secret": "s2sSecret", "room_user": "room1@crystalpm.com"}
SDK_ONLY = {"sdk_client_id": "sdkClient123", "sdk_client_secret": "sdkSecret456"}


class FakeApi:
    def __init__(self, account_id, client_id, client_secret, token_error=None, zak_error=None):
        self.args = (account_id, client_id, client_secret)
        self.token_error, self.zak_error = token_error, zak_error
        self.zak_calls = []

    async def access_token(self):
        if self.token_error:
            raise ZoomAuthError(self.token_error)
        return "ACCESS"

    async def user_zak(self, user, ttl_seconds=7200):
        self.zak_calls.append((user, ttl_seconds))
        if self.zak_error:
            raise ZoomAuthError(self.zak_error)
        return "ZAK"


def config_for(tmp_path, credentials):
    config = Config()
    if credentials is not None:
        path = tmp_path / "zoom-credentials.json"
        path.write_text(json.dumps(credentials))
        config.meeting.zoom_credentials_path = str(path)
    return config


async def run(config, **api_kwargs):
    out = io.StringIO()
    made = []

    def factory(account_id, client_id, client_secret):
        api = FakeApi(account_id, client_id, client_secret, **api_kwargs)
        made.append(api)
        return api

    code = await check_zoom(config, out=out, api_factory=factory)
    return code, out.getvalue(), made


class TestSuccess:
    async def test_full_credentials(self, tmp_path):
        code, text, made = await run(config_for(tmp_path, FULL))
        assert code == 0, text
        assert "Zoom Meeting SDK: app sdkClient123, signature minted" in text
        assert "Zoom account: server-to-server token obtained for account acct789" in text
        assert "Zoom room user: room1@crystalpm.com, ZAK obtained (valid 2 hours)" in text
        assert text.rstrip().endswith("Ready: this room can join meetings hosted by any Zoom account.")
        [api] = made
        assert api.args == ("acct789", "s2sClient", "s2sSecret") and api.zak_calls == [("room1@crystalpm.com", 7200)]

    async def test_sdk_only(self, tmp_path):
        code, text, made = await run(config_for(tmp_path, SDK_ONLY))
        assert code == 0, text
        assert "Zoom room user: not configured; this room can join only meetings hosted on your own Zoom account." in text
        assert made == []


class TestFailures:
    async def test_not_configured(self, tmp_path):
        code, text, made = await run(config_for(tmp_path, None))
        assert code == 1 and "Zoom Meeting SDK not configured: no zoom_credentials_path in the config" in text

    async def test_placeholder(self, tmp_path):
        code, text, _ = await run(config_for(tmp_path, dict(FULL, sdk_client_id="REPLACE_WITH_CLIENT_ID")))
        assert code == 1 and "sdk_client_id is missing or still REPLACE" in text

    async def test_token_refused(self, tmp_path):
        words = "Zoom refused the server-to-server credentials: check the account id, client id and client secret, and that the app is activated"
        code, text, _ = await run(config_for(tmp_path, FULL), token_error=words)
        assert code == 1 and f"Zoom: {words}" in text and "ZAK obtained" not in text

    async def test_zak_refused(self, tmp_path):
        code, text, _ = await run(config_for(tmp_path, FULL), zak_error="Zoom has no user room1@crystalpm.com on this account")
        assert code == 1 and "Zoom: Zoom has no user room1@crystalpm.com on this account" in text
        assert "server-to-server token obtained" in text


class TestCommandLine:
    def test_help_names_the_flag(self):
        result = subprocess.run([sys.executable, "-m", "croom.core.agent", "--help"], capture_output=True, text=True)
        assert result.returncode == 0 and "--check-zoom" in result.stdout

    def test_flag_runs_the_check(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump({"meeting": {"platforms": ["zoom"], "zoom_credentials_path": str(tmp_path / "gone.json")}}))
        result = subprocess.run([sys.executable, "-m", "croom.core.agent", "--check-zoom", "-c", str(cfg)],
                                capture_output=True, text=True)
        assert result.returncode == 1
        assert "credentials file not found or unreadable" in result.stdout
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/meeting/test_zoom_check.py -q -p no:cacheprovider`
Expected: FAIL: `ModuleNotFoundError: No module named 'croom.meeting.zoom_check'`.

- [ ] **Step 3: Write the check module and the flag**

Create `src/croom/meeting/zoom_check.py`:

```python
"""
`croom --check-zoom`: prove the Zoom credentials on this device work, in plain
words (spec 2026-09-25 Zoom, section 4.6). Exit 0 when every configured piece
works, 1 otherwise.
"""

import sys
from typing import Callable, Optional, TextIO

from croom.core.config import Config
from croom.meeting.zoom_auth import (
    ZoomApi,
    ZoomAuthError,
    load_zoom_credentials,
    meeting_sdk_signature,
    zoom_not_configured_reason,
)

TEST_MEETING_NUMBER = "1234567890"


async def check_zoom(config: Config, out: TextIO = sys.stdout,
                     api_factory: Optional[Callable[[str, str, str], ZoomApi]] = None) -> int:
    """Mint a signature, then prove the server-to-server credential and the room user's ZAK when configured."""
    path = config.meeting.zoom_credentials_path
    reason = zoom_not_configured_reason(path)
    if reason:
        print(f"Zoom Meeting SDK not configured: {reason}", file=out)
        return 1
    credentials = load_zoom_credentials(path)
    meeting_sdk_signature(credentials.sdk_client_id, credentials.sdk_client_secret, TEST_MEETING_NUMBER)
    print(f"Zoom Meeting SDK: app {credentials.sdk_client_id}, signature minted", file=out)
    if not credentials.has_room_user:
        print("Zoom room user: not configured; this room can join only meetings hosted on your own Zoom account.", file=out)
        return 0
    api = (api_factory or ZoomApi)(credentials.account_id, credentials.s2s_client_id, credentials.s2s_client_secret)
    try:
        await api.access_token()
        print(f"Zoom account: server-to-server token obtained for account {credentials.account_id}", file=out)
        await api.user_zak(credentials.room_user, ttl_seconds=7200)
        print(f"Zoom room user: {credentials.room_user}, ZAK obtained (valid 2 hours)", file=out)
    except ZoomAuthError as e:
        print(f"Zoom: {e}", file=out)
        return 1
    print("Ready: this room can join meetings hosted by any Zoom account.", file=out)
    return 0
```

In `src/croom/core/agent.py`, in `main()`, after the `--check-calendar` `parser.add_argument(...)` call add:

```python
    parser.add_argument(
        "--check-zoom",
        help="Check the room's Zoom Meeting SDK credentials and exit",
        action="store_true"
    )
```

and directly after the `if args.check_calendar:` block add:

```python
    if args.check_zoom:
        from croom.meeting.zoom_check import check_zoom
        raise SystemExit(asyncio.run(check_zoom(load_config(args.config))))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/meeting/test_zoom_check.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Try the command on this PC**

Run: `.venv/bin/croom --check-zoom -c deploy/rooms/room-1.yaml; echo "exit $?"`
Expected (Task 6 has not run yet): `Zoom Meeting SDK not configured: no zoom_credentials_path in the config` and `exit 1`.

- [ ] **Step 6: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 7: Commit**

```bash
git add src/croom/meeting/zoom_check.py src/croom/core/agent.py tests/unit/meeting/test_zoom_check.py
git commit -m "feat(meeting): croom --check-zoom explains the Zoom credentials in plain words"
```

---

### Task 6: Installer `--zoom-credentials`, the room configs, the credentials template and the rooms README

**Files:**
- Modify: `installer/install.sh` (variables, `install_credentials` split into a shared helper, new `install_zoom_credentials`, `main`, `run_installer`, `print_completion`), `.gitignore`
- Modify: `deploy/rooms/room-1.yaml`, `deploy/rooms/room-2.yaml`, `deploy/rooms/room-3.yaml`, `deploy/rooms/README.md`
- Create: `deploy/rooms/zoom-credentials.example.json`
- Test: `tests/unit/installer/test_install_script.py`, `tests/unit/deploy/test_room_configs.py`

**Interfaces:**
- Consumes: `zoom_not_configured_reason` (Task 1) in the deploy test; the check command name (Task 5).
- Produces: `installer/install.sh --zoom-credentials FILE`, functions `install_private_file SRC DEST LABEL` and `install_zoom_credentials`, variable `ZOOM_CREDENTIALS_FILE`; the configs' `meeting.zoom_credentials_path`; the template the guide (Task 7) tells admins to copy.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/installer/test_install_script.py` replace

```python
def test_credentials_are_never_world_readable_even_briefly():
    body = SCRIPT.read_text().split("install_credentials() {")[1].split("\n}\n")[0]
    assert 'install -o "$CROOM_USER" -g "$CROOM_USER" -m 600' in body
    assert "cp " not in body
```

with

```python
def test_credentials_are_never_world_readable_even_briefly():
    body = SCRIPT.read_text().split("install_private_file() {")[1].split("\n}\n")[0]
    assert 'install -o "$CROOM_USER" -g "$CROOM_USER" -m 600' in body
    assert "cp " not in body
    for function in ("install_credentials() {", "install_zoom_credentials() {"):
        caller = SCRIPT.read_text().split(function)[1].split("\n}\n")[0]
        assert "install_private_file" in caller
```

and append:

```python


def test_missing_zoom_credentials_file_is_refused_before_install(tmp_path):
    result = run_bash(f"bash {SCRIPT} --zoom-credentials {tmp_path / 'nope.json'}")
    assert result.returncode == 1
    assert "not found" in (result.stdout + result.stderr).lower()


def test_help_mentions_zoom_credentials():
    assert "--zoom-credentials FILE" in run_bash(f"bash {SCRIPT} --help").stdout


def test_zoom_credentials_are_installed_for_the_service_user_only(tmp_path):
    key = tmp_path / "zoom.json"
    key.write_text('{"sdk_client_id": "a", "sdk_client_secret": "b"}')
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={tmp_path / 'etc'}; CROOM_USER=$(id -un); ZOOM_CREDENTIALS_FILE={key}; install_zoom_credentials",
    )
    assert result.returncode == 0, result.stderr
    installed = tmp_path / "etc" / "zoom-credentials.json"
    assert installed.read_text() == key.read_text()
    assert oct(installed.stat().st_mode & 0o777) == "0o600"


def test_reinstalling_with_the_installed_zoom_credentials_keeps_them(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    installed = etc / "zoom-credentials.json"
    installed.write_text('{"sdk_client_id": "a", "sdk_client_secret": "b"}')
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={etc}; CROOM_USER=$(id -un); ZOOM_CREDENTIALS_FILE={installed}; install_zoom_credentials",
    )
    assert result.returncode == 0, result.stderr
    assert installed.read_text() == '{"sdk_client_id": "a", "sdk_client_secret": "b"}'


def test_completion_message_names_the_zoom_check_when_zoom_credentials_were_installed():
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG=/tmp/room.yaml; ZOOM_CREDENTIALS_FILE=/tmp/zoom.json; print_completion")
    assert result.returncode == 0, result.stderr
    assert "croom --check-zoom -c /etc/croom/config.yaml" in result.stdout
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG=/tmp/room.yaml; print_completion")
    assert "--check-zoom" not in result.stdout
```

In `tests/unit/deploy/test_room_configs.py` replace

```python
    assert config.calendar.google_calendar_id == "REPLACE_WITH_ROOM_CALENDAR_ID"
```

with

```python
    assert config.calendar.google_calendar_id == "REPLACE_WITH_ROOM_CALENDAR_ID"
    assert config.meeting.zoom_credentials_path == "/etc/croom/zoom-credentials.json"
```

and append:

```python


def test_zoom_credentials_template_is_all_placeholders():
    from croom.meeting.zoom_auth import zoom_not_configured_reason
    template = Path(__file__).resolve().parents[3] / "deploy" / "rooms" / "zoom-credentials.example.json"
    data = json.loads(template.read_text(encoding="utf-8"))
    assert set(data) == {"sdk_client_id", "sdk_client_secret", "account_id", "s2s_client_id", "s2s_client_secret", "room_user"}
    assert all(value.startswith("REPLACE_") for value in data.values())
    assert zoom_not_configured_reason(str(template)).startswith("sdk_client_id is missing or still REPLACE")


def test_git_ignores_real_zoom_credentials():
    repo = Path(__file__).resolve().parents[3]
    for name in ("zoom-credentials.json", "deploy/rooms/zoom-credentials.json"):
        assert subprocess.run(["git", "check-ignore", "-q", name], cwd=repo).returncode == 0, name
    assert subprocess.run(["git", "check-ignore", "-q", "deploy/rooms/zoom-credentials.example.json"], cwd=repo).returncode == 1
```

and add `import json` to the imports at the top of that file (it already imports `subprocess`, `Path`, `pytest`, `yaml` and `Config`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/installer/test_install_script.py tests/unit/deploy -q -p no:cacheprovider`
Expected: FAIL: the world-readable test (no `install_private_file`), the `--zoom-credentials` run exits with the root error, `--help` lacks the option, `install_zoom_credentials` is not a function, the completion message lacks the check, the configs lack `zoom_credentials_path`, the template is missing, and `zoom-credentials.json` is not ignored.

- [ ] **Step 3: Change the installer and .gitignore**

In `installer/install.sh`:

After `CREDENTIALS_FILE=""` add `ZOOM_CREDENTIALS_FILE=""`.

Replace the whole `install_credentials()` function (from `# Install the Google service account key where the agent reads it (calendar spec 4.5)` through its closing `}`) with:

```bash
# Copy a secret file into place for the service user only (mode 600). A reinstall
# may name the already installed file itself; keep it and fix owner and mode.
install_private_file() {
    local source="$1" target="$2" label="$3"
    mkdir -p "$CONFIG_DIR"
    if [[ "$source" -ef "$target" ]]; then
        chown "$CROOM_USER:$CROOM_USER" "$target"
        chmod 600 "$target"
    else
        # install(1) creates the file with its final owner and mode, never world-readable
        install -o "$CROOM_USER" -g "$CROOM_USER" -m 600 "$source" "$target"
    fi
    log "Installed $label at $target"
}

# The Google service account key (calendar spec 4.5)
install_credentials() {
    if [[ -z "$CREDENTIALS_FILE" ]]; then
        return
    fi
    install_private_file "$CREDENTIALS_FILE" "$CONFIG_DIR/google-service-account.json" "Google Calendar credentials"
}

# The Zoom Meeting SDK credentials (Zoom spec 4.7)
install_zoom_credentials() {
    if [[ -z "$ZOOM_CREDENTIALS_FILE" ]]; then
        return
    fi
    install_private_file "$ZOOM_CREDENTIALS_FILE" "$CONFIG_DIR/zoom-credentials.json" "Zoom credentials"
}
```

In `main()` replace `    install_credentials\n    create_service\n` with `    install_credentials\n    install_zoom_credentials\n    create_service\n`.

In `run_installer()` add this case directly after the `--credentials)` case block:

```bash
            --zoom-credentials)
                ZOOM_CREDENTIALS_FILE="$2"
                if [[ -z "$ZOOM_CREDENTIALS_FILE" || ! -f "$ZOOM_CREDENTIALS_FILE" ]]; then
                    error "Zoom credentials file not found: ${ZOOM_CREDENTIALS_FILE:-<missing>}"
                fi
                shift 2
                ;;
```

and in the `--help` text, directly after the `--credentials FILE` line, add:

```bash
                echo "  --zoom-credentials FILE  Install Zoom Meeting SDK credentials as /etc/croom/zoom-credentials.json"
```

In `print_completion()` replace

```bash
    if [[ -n "$CREDENTIALS_FILE" ]]; then
        echo ""
        echo "Check the calendar: $INSTALL_DIR/venv/bin/croom --check-calendar -c $CONFIG_DIR/config.yaml"
    fi
```

with

```bash
    if [[ -n "$CREDENTIALS_FILE" ]]; then
        echo ""
        echo "Check the calendar: $INSTALL_DIR/venv/bin/croom --check-calendar -c $CONFIG_DIR/config.yaml"
    fi
    if [[ -n "$ZOOM_CREDENTIALS_FILE" ]]; then
        echo ""
        echo "Check Zoom: $INSTALL_DIR/venv/bin/croom --check-zoom -c $CONFIG_DIR/config.yaml"
    fi
```

In `.gitignore` replace

```
# Google service account keys are installed on devices, never committed
google-service-account.json
```

with

```
# Credential files are installed on devices, never committed
google-service-account.json
zoom-credentials.json
```

Run: `bash -n installer/install.sh`
Expected: no output.

- [ ] **Step 4: Change the room configs, add the template, update the rooms README**

In each of `deploy/rooms/room-1.yaml`, `room-2.yaml`, `room-3.yaml` replace the header comment lines

```yaml
# Replace the four REPLACE values, then install with:
#   sudo bash installer/install.sh --config room-N.yaml --credentials google-service-account.json
```

(N is the room number) with

```yaml
# Replace the four REPLACE values, then install with:
#   sudo bash installer/install.sh --config room-N.yaml --credentials google-service-account.json --zoom-credentials zoom-credentials.json
```

and replace

```yaml
  camera_default_on: true
  mic_default_on: true
```

with

```yaml
  camera_default_on: true
  mic_default_on: true
  zoom_credentials_path: /etc/croom/zoom-credentials.json
```

Create `deploy/rooms/zoom-credentials.example.json`:

```json
{
  "sdk_client_id": "REPLACE_WITH_MEETING_SDK_CLIENT_ID",
  "sdk_client_secret": "REPLACE_WITH_MEETING_SDK_CLIENT_SECRET",
  "account_id": "REPLACE_WITH_ACCOUNT_ID",
  "s2s_client_id": "REPLACE_WITH_SERVER_TO_SERVER_CLIENT_ID",
  "s2s_client_secret": "REPLACE_WITH_SERVER_TO_SERVER_CLIENT_SECRET",
  "room_user": "REPLACE_WITH_ROOM_ZOOM_USER_EMAIL"
}
```

In `deploy/rooms/README.md` replace

```markdown
Install on the device with the room config and the service account key:

    sudo bash installer/install.sh --config /path/to/room-N.yaml --credentials /path/to/google-service-account.json

The key is copied to `/etc/croom/google-service-account.json`, readable only by
the service user. Without `--credentials` the room works with pasted links only
and logs one line saying the calendar is not configured.
```

with

```markdown
Zoom needs one more file per room, `zoom-credentials.json`, made from
`zoom-credentials.example.json`: the Meeting SDK app's client id and secret
(the same for every room), and, to join meetings hosted by other Zoom accounts,
the Server-to-Server app's account id, client id and secret plus that room's own
Zoom user (`room_user`, for example `room1@crystalpm.com`). The guide "Connect
Crystal Meet rooms to Zoom" covers creating all of it.

Install on the device with the room config and both credential files:

    sudo bash installer/install.sh --config /path/to/room-N.yaml --credentials /path/to/google-service-account.json --zoom-credentials /path/to/zoom-credentials.json

The files are copied to `/etc/croom/google-service-account.json` and
`/etc/croom/zoom-credentials.json`, readable only by the service user. Without
`--credentials` the room works with pasted links only and logs one line saying
the calendar is not configured; without `--zoom-credentials` Zoom joins fall
back to the public web client, which Zoom blocks for automated guests.
```

and replace

```markdown
`/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml`.
Never commit a real token or key to this folder.
```

with

```markdown
`/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml` and Zoom
with `/opt/croom/venv/bin/croom --check-zoom -c /etc/croom/config.yaml`.
Never commit a real token, key or secret to this folder.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `bash -n installer/install.sh && .venv/bin/pytest tests/unit/installer/test_install_script.py tests/unit/deploy -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: See the check command's words on an unfilled config**

Run: `.venv/bin/croom --check-zoom -c deploy/rooms/room-1.yaml; echo "exit $?"`
Expected: `Zoom Meeting SDK not configured: credentials file not found or unreadable: /etc/croom/zoom-credentials.json` and `exit 1`.

- [ ] **Step 7: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 8: Commit**

```bash
git add installer/install.sh .gitignore deploy tests/unit/installer/test_install_script.py tests/unit/deploy/test_room_configs.py
git commit -m "feat(deploy): installer --zoom-credentials, the Zoom credentials template and the config key"
```

---

### Task 7: The Zoom guide and the README

**Files:**
- Create: `docs/guides/crystal-meet-zoom/index.html`, `docs/guides/crystal-meet-zoom/build.py`, `docs/guides/crystal-meet-zoom/crystalpm-logo-white.svg`, `docs/guides/crystal-meet-zoom/fonts/{lexend-400.woff2,lexend-600.woff2,OFL.txt}`, `docs/guides/crystal-meet-zoom.pdf`, `tests/unit/docs/test_zoom_guide.py`
- Modify: `docs/guides/crystal-meet-room-setup/index.html`, `docs/guides/crystal-meet-room-setup.pdf`, `README.md`, `tests/unit/docs/test_readme.py`

**Interfaces:**
- Consumes: the installer option, template, config key and check command from Tasks 5 and 6 (quoted exactly: `--zoom-credentials`, `zoom-credentials.json`, `/opt/croom/venv/bin/croom --check-zoom -c /etc/croom/config.yaml`, `user:read:token:admin`, `deploy/rooms/zoom-credentials.example.json`); `docs/guides/render_guide.py` (existing).
- Produces: the guide PDF and README text later work extends.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/docs/test_zoom_guide.py`:

```python
"""
The Zoom guide builds to a multi-page PDF, is self-contained, quotes the real
commands and names, and the setup guide points at it.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GUIDES = REPO / "docs" / "guides"
GUIDE = GUIDES / "crystal-meet-zoom"

pytest.importorskip("playwright.sync_api")


def test_zoom_guide_builds_to_a_multi_page_pdf(tmp_path):
    out = tmp_path / "guide.pdf"
    result = subprocess.run([sys.executable, str(GUIDE / "build.py"), str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    counts = [int(m) for m in re.findall(rb"/Count (\d+)", data)]
    assert counts and max(counts) >= 3, counts


def test_zoom_guide_is_self_contained():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "url(http" not in html and "@import" not in html
    assert "Crystal Meet" in html and "Croom " not in html
    for asset in ("crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2", "fonts/OFL.txt"):
        assert (GUIDE / asset).is_file(), asset


def test_zoom_guide_quotes_the_real_commands_and_names():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    for needle in ("--zoom-credentials", "zoom-credentials.json", "zoom-credentials.example.json",
                   "croom --check-zoom -c /etc/croom/config.yaml", "user:read:token:admin",
                   "Meeting SDK", "Server-to-Server OAuth", "room_user"):
        assert needle in html, needle


def test_setup_guide_points_to_the_zoom_guide():
    html = (GUIDES / "crystal-meet-room-setup" / "index.html").read_text(encoding="utf-8")
    assert "Connect Crystal Meet rooms to Zoom" in html


def test_zoom_guide_uses_the_shared_renderer():
    assert "from render_guide import render" in (GUIDE / "build.py").read_text(encoding="utf-8")
```

In `tests/unit/docs/test_readme.py` replace

```python
        "docs/guides/crystal-meet-google-calendar.pdf",
```

with

```python
        "docs/guides/crystal-meet-google-calendar.pdf",
        "docs/guides/crystal-meet-zoom.pdf",
        "--zoom-credentials",
        "croom --check-zoom",
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/docs -q -p no:cacheprovider`
Expected: the five Zoom guide tests and `test_readme_names_the_setup_pieces` and `test_readme_links_every_spec_and_plan` FAIL; the other docs tests pass.

- [ ] **Step 3: Create the guide**

```bash
G=docs/guides/crystal-meet-zoom
mkdir -p "$G/fonts"
cp docs/guides/crystal-meet-google-calendar/crystalpm-logo-white.svg "$G/"
cp docs/guides/crystal-meet-google-calendar/fonts/lexend-400.woff2 docs/guides/crystal-meet-google-calendar/fonts/lexend-600.woff2 docs/guides/crystal-meet-google-calendar/fonts/OFL.txt "$G/fonts/"
```

Create `docs/guides/crystal-meet-zoom/build.py`:

```python
"""
Render the Zoom guide to PDF.

Usage: python build.py [output.pdf]   (default: ../crystal-meet-zoom.pdf)
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from render_guide import render  # noqa: E402

if __name__ == "__main__":
    render(HERE, Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "crystal-meet-zoom.pdf",
           "How-to guide · Connect Crystal Meet rooms to Zoom")
```

Create `docs/guides/crystal-meet-zoom/index.html` with the calendar guide's `<style>` block copied verbatim from `docs/guides/crystal-meet-google-calendar/index.html` (everything between `<style>` and `</style>`, including the five-column `.glance`), and this body:

```html
<section class="banner">
  <img src="crystalpm-logo-white.svg" alt="Crystal PM">
  <p class="kicker top">Crystal PM</p>
  <p class="kicker">How-to guide</p>
  <h1>Connect Crystal Meet rooms to Zoom</h1>
  <p>Let each room join Zoom meetings through Zoom's Meeting SDK, including meetings hosted by customers and vendors, with no one signing in on the device.</p>
</section>

<p class="intro">This guide has two halves. The Zoom half is done once for all rooms by the person who administers your Zoom account: two apps in the App Marketplace and one Zoom user per room, about 20 minutes. The device half takes about five minutes per room. Rooms still join only when someone presses Join on the room page.</p>

<div class="glance">
  <div class="card"><p class="kicker">Step 1</p><h3>The SDK app</h3><p>A Meeting SDK app and its client id and secret.</p></div>
  <div class="card"><p class="kicker">Step 2</p><h3>The token app</h3><p>A Server-to-Server OAuth app with one scope.</p></div>
  <div class="card"><p class="kicker">Step 3</p><h3>A user per room</h3><p>Rooms join outside meetings as their own Zoom user.</p></div>
  <div class="card"><p class="kicker">Step 4</p><h3>Put it on the device</h3><p>The credentials file, the installer, the check.</p></div>
  <div class="card"><p class="kicker">Step 5</p><h3>Join a test meeting</h3><p>One inside your account, one outside.</p></div>
</div>

<div class="callout warn">
  <p class="kicker">Before you begin</p>
  <p><strong>Access.</strong> The Zoom account owner, or an admin with the Develop permission (visible as <span class="ui">Develop</span> at the top of the App Marketplace) and rights to add users.</p>
  <p><strong>Why three parts.</strong> Since March 2026 Zoom lets an SDK app join meetings hosted by other Zoom accounts only as a signed-in Zoom user. The SDK app (step 1) joins your own meetings; the token app (step 2) and a Zoom user per room (step 3) let the room join everyone else's.</p>
  <p><strong>Secrets.</strong> You will collect a client id and secret (step 1) and an account id, client id and secret (step 2). Keep them out of email and chat; they go straight into a file on each device.</p>
</div>

<section class="block">
<div class="step"><span class="badge">1</span><h2>Create the Meeting SDK app</h2></div>
<ul>
  <li><strong>Open the App Marketplace</strong> at <span class="chip">marketplace.zoom.us</span> signed in as the admin, then <span class="ui">Develop › Build App</span>, choose <span class="ui">General App</span> and create it. Name it <span class="chip">Crystal Meet rooms</span>.</li>
  <li><strong>Turn on the Meeting SDK.</strong> In the app's <span class="ui">Features</span> (or <span class="ui">Embed</span>) section, enable <span class="ui">Meeting SDK</span>.</li>
  <li><strong>Copy the credentials.</strong> On <span class="ui">Basic Information › App Credentials</span>, copy the <span class="ui">Client ID</span> and <span class="ui">Client Secret</span>. The development credentials are fine: the app is used only inside your account and is never published.</li>
</ul>
<p class="see">You should now see the app under Manage › Built Apps with Meeting SDK enabled, and have its client id and secret.</p>
</section>

<section class="block">
<div class="step"><span class="badge">2</span><h2>Create the Server-to-Server app for tokens</h2></div>
<ul>
  <li><strong>Build it.</strong> <span class="ui">Develop › Build App</span>, choose <span class="ui">Server-to-Server OAuth</span>, name it <span class="chip">Crystal Meet room tokens</span>.</li>
  <li><strong>Copy the credentials</strong> from <span class="ui">App Credentials</span>: <span class="ui">Account ID</span>, <span class="ui">Client ID</span> and <span class="ui">Client Secret</span>.</li>
  <li><strong>Add the one scope.</strong> Under <span class="ui">Scopes › Add Scopes</span>, search for <span class="chip">token</span> and add <span class="chip">user:read:token:admin</span> (View users' tokens). Nothing else is needed.</li>
  <li><strong>Activate the app</strong> on the <span class="ui">Activation</span> page. Tokens only work while it is active.</li>
</ul>
<p class="see">You should now see the app listed as active, with the account id, client id and secret saved with the step 1 values.</p>
</section>

<section class="block">
<div class="step"><span class="badge">3</span><h2>Create a Zoom user for each room</h2></div>
<ul>
  <li><strong>Add the user.</strong> In <span class="chip">admin.zoom.us</span> go to <span class="ui">User Management › Users › Add Users</span>. Use a mailbox you can read, for example <span class="chip">room1@crystalpm.com</span>, and the <span class="ui">Basic</span> user type, which is free on a paid account.</li>
  <li><strong>Activate it.</strong> Open the invitation email in that mailbox and finish the sign-up.</li>
  <li><strong>Name it after the room.</strong> In the user's profile set the display name to the room's name, for example <span class="chip">Room 1</span>. That is the name hosts see when the room joins.</li>
  <li><strong>Repeat</strong> for Room 2 and Room 3.</li>
</ul>
<p class="see">You should now see one active Basic user per room, each named after its room.</p>
<div class="callout">
  <p class="kicker">Good to know</p>
  <p>The room user never hosts anything and needs no licence. If you skip steps 2 and 3, rooms can still join meetings hosted on your own Zoom account; they just cannot join meetings hosted by other companies.</p>
</div>
</section>

<section class="block">
<div class="step"><span class="badge">4</span><h2>Put the credentials on each device</h2></div>
<ul>
  <li><strong>Make the file.</strong> Copy <span class="chip">deploy/rooms/zoom-credentials.example.json</span> to <span class="chip">zoom-credentials.json</span> and fill in the six values: the step 1 client id and secret, the step 2 account id, client id and secret, and that room's user as <span class="ui">room_user</span>. Each room gets its own file because <span class="ui">room_user</span> differs.</li>
  <li><strong>Copy it to the device</strong>, for example <span class="chip wrap">scp zoom-credentials.json pi@crystal-meet-room-1.local:~/</span>.</li>
  <li><strong>A device you are installing now.</strong> Add <span class="chip">--zoom-credentials ~/zoom-credentials.json</span> to the installer command from the setup guide.</li>
  <li><strong>A device that is already installed.</strong> Type <span class="chip wrap">sudo install -o pi -g pi -m 600 ~/zoom-credentials.json /etc/croom/zoom-credentials.json</span> (your username instead of pi), then <span class="chip">sudo systemctl restart croom</span>.</li>
  <li><strong>Check it.</strong> Type <span class="chip wrap">/opt/croom/venv/bin/croom --check-zoom -c /etc/croom/config.yaml</span>. It reads:</li>
</ul>
<pre>Zoom Meeting SDK: app AbC123xYz, signature minted
Zoom account: server-to-server token obtained for account 9XyZ12ab
Zoom room user: room1@crystalpm.com, ZAK obtained (valid 2 hours)
Ready: this room can join meetings hosted by any Zoom account.</pre>
<ul>
  <li><strong>Delete the copy in your home folder.</strong> Type <span class="chip">rm ~/zoom-credentials.json</span>.</li>
</ul>
<p class="see">You should now see the check end with "Ready", and the room's log say the Zoom Meeting SDK provider is ready.</p>
</section>

<section class="block">
<div class="step"><span class="badge">5</span><h2>Join a test meeting</h2></div>
<ul>
  <li><strong>Inside your account.</strong> Have a colleague start a Zoom meeting and paste its invitation link, passcode included, into the room page's <span class="ui">Join with a link</span>, or book it on the room's calendar and press <span class="ui">Join now</span>. Zoom's meeting appears on the TV within about fifteen seconds.</li>
  <li><strong>Outside your account.</strong> Join a meeting hosted by someone outside the company, or by a personal Zoom account. The room appears in the participant list under its own name, for example Room 1. If the host uses a waiting room, the room page says it is waiting until the host admits it.</li>
  <li><strong>Controls.</strong> From the table page press <span class="ui">Mute</span>, <span class="ui">Turn camera off</span>, then <span class="ui">Leave</span> and <span class="ui">Tap again to leave</span>.</li>
</ul>
<p class="see">You should now see both meetings join, the room named after itself in the outside one, and the TV return to the room's idle screen after leaving.</p>
</section>

<div class="page-break"></div>
<section class="block">
<p class="kicker">If something is off</p>
<h2>Troubleshooting</h2>
<div class="trouble">
  <div class="card"><h3>The check says "not configured"</h3><p>It names the value that is missing or still a placeholder in <span class="chip">/etc/croom/zoom-credentials.json</span>. The four server-to-server values and <span class="ui">room_user</span> go together: fill in all of them or none.</p></div>
  <div class="card"><h3>The check says Zoom refused the credentials</h3><p>The account id, client id or secret of the step 2 app is wrong, or the app is not activated. Copy them again from App Credentials and check the Activation page.</p></div>
  <div class="card"><h3>The check says no such user, or a scope is missing</h3><p>The <span class="ui">room_user</span> must be an active user on this Zoom account. "Lacks the user token scope" means step 2's scope <span class="chip">user:read:token:admin</span> is missing: add it and activate the app again.</p></div>
  <div class="card"><h3>The room page says the meeting is not authorized</h3><p>The meeting is hosted by another account and the room joined without its Zoom user. Fill in the server-to-server values and <span class="ui">room_user</span>, restart the service and run the check.</p></div>
  <div class="card"><h3>Wrong passcode or meeting not found</h3><p>Paste the full invitation link with its passcode part. A bare meeting id cannot open a passcode-protected meeting. Zoom's exact reason shows on the room page.</p></div>
  <div class="card"><h3>The TV stays dark and the page says the SDK did not load</h3><p>The device could not reach <span class="chip">source.zoom.us</span>. Check the device's internet access and any web filter, then try again.</p></div>
</div>
</section>
<div class="callout">
  <p class="kicker">Good to know</p>
  <p>The room joins meetings as its own Zoom user through Zoom's supported Meeting SDK, so it is never mistaken for a bot. To take that away, deactivate the two apps or the room users in Zoom.</p>
</div>
```

Wrap the body in the same `<!DOCTYPE html>`, `<html lang="en">`, `<head>` (charset, `<title>Connect Crystal Meet rooms to Zoom</title>`, the copied style) and `<body>` structure as the calendar guide.

In `docs/guides/crystal-meet-room-setup/index.html`, directly after the bullet that starts with `<li><strong>Connect the calendar.</strong>` add:

```html
  <li><strong>Connect Zoom.</strong> To join Zoom meetings, including ones hosted by other companies, follow the guide <span class="ui">Connect Crystal Meet rooms to Zoom</span>. Without it, Zoom turns the room away as an automated guest.</li>
```

- [ ] **Step 4: Update the README**

In `README.md`:

Replace

```markdown
2. [Connect Crystal Meet rooms to Google Calendar](docs/guides/crystal-meet-google-calendar.pdf):
   room resources in Google Workspace, one service account and key, share each
   room calendar with it, put the key and the calendar address on the device.
```

with

```markdown
2. [Connect Crystal Meet rooms to Google Calendar](docs/guides/crystal-meet-google-calendar.pdf):
   room resources in Google Workspace, one service account and key, share each
   room calendar with it, put the key and the calendar address on the device.
3. [Connect Crystal Meet rooms to Zoom](docs/guides/crystal-meet-zoom.pdf): a
   Meeting SDK app, a Server-to-Server app for tokens, one Zoom user per room,
   and the credentials file on the device, so rooms join any Zoom meeting.
```

Replace

```bash
sudo bash installer/install.sh --config ~/room.yaml --credentials ~/google-service-account.json
sudo systemctl start croom
/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml
```

with

```bash
sudo bash installer/install.sh --config ~/room.yaml --credentials ~/google-service-account.json --zoom-credentials ~/zoom-credentials.json
sudo systemctl start croom
/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml
/opt/croom/venv/bin/croom --check-zoom -c /etc/croom/config.yaml
```

Replace

```markdown
`/etc/croom/config.yaml`; `--credentials FILE` installs a Google service
account key as `/etc/croom/google-service-account.json`, readable only by the
service user; `--enable-ui` and `--no-service` are upstream options; the
```

with

```markdown
`/etc/croom/config.yaml`; `--credentials FILE` installs a Google service
account key as `/etc/croom/google-service-account.json` and
`--zoom-credentials FILE` the Zoom credentials as
`/etc/croom/zoom-credentials.json`, both readable only by the service user;
`--enable-ui` and `--no-service` are upstream options; the
```

Replace the `meeting` row of the configure table

```markdown
| `meeting` | `platforms: [zoom, google_meet]` | Which links the room can join; joins are limited to those platforms' hostnames. |
```

with

```markdown
| `meeting` | `platforms: [zoom, google_meet]`, `zoom_credentials_path` | Which links the room can join; joins are limited to those platforms' hostnames. Zoom joins go through Zoom's Meeting SDK with the credentials file (`deploy/rooms/zoom-credentials.example.json`); without it the public web client is used, and Zoom blocks automated guests there. |
```

In the implementation notes table add, after the Google Calendar row:

```markdown
| Zoom joins through the Meeting SDK: signature, per-room Zoom user's ZAK for outside hosts, loopback page, `croom --check-zoom`, the Zoom guide | [spec](docs/superpowers/specs/2026-09-25-zoom-meeting-sdk-design.md) | [plan](docs/superpowers/plans/2026-09-25-zoom-meeting-sdk.md) |
```

In "Decisions worth knowing" add a bullet after the calendar bullet:

```markdown
- Zoom is joined through Zoom's Meeting SDK, never by driving the public web client, which blocks automated guests. Meetings on your own account need only the SDK app's signature; meetings hosted elsewhere need the room's Zoom user and its ZAK, fetched with the Server-to-Server credential.
```

- [ ] **Step 5: Build the PDFs and run the tests**

Run: `.venv/bin/python docs/guides/crystal-meet-zoom/build.py && .venv/bin/python docs/guides/crystal-meet-room-setup/build.py && .venv/bin/pytest tests/unit/docs -q -p no:cacheprovider`
Expected: two `wrote ...` lines and all docs tests pass.

- [ ] **Step 6: Look at the Zoom guide**

Render each page to an image (pymupdf, 96 dpi) and check: kicker labels present, the five glance cards on one row and readable, no orphaned step headers, the check output block readable, six troubleshooting cards, footer with page numbers on every page, headlines in sentence case. Fix and rebuild before committing.

- [ ] **Step 7: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 8: Commit**

```bash
git add docs/guides/crystal-meet-zoom docs/guides/crystal-meet-zoom.pdf docs/guides/crystal-meet-room-setup docs/guides/crystal-meet-room-setup.pdf README.md tests/unit/docs/test_zoom_guide.py tests/unit/docs/test_readme.py
git commit -m "docs: Zoom guide for Crystal Meet rooms and README updates"
```

---

### Task 8: Push and final check

**Files:** none new.

- [ ] **Step 1: Confirm nothing user-facing says Croom and no secret slipped in**

Run: `grep -rn "Croom" deploy docs/guides/crystal-meet-zoom/index.html src/croom/meeting/zoom_check.py src/croom/meeting/providers/zoom_sdk_page; git grep -l "BEGIN PRIVATE KEY\|client_secret\": \"[A-Za-z0-9]" -- . ':!*node_modules*' ':!docs/superpowers*'`
Expected: no output from either.

- [ ] **Step 2: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 3: Push**

```bash
git push -u origin zoom-sdk
```

- [ ] **Step 4: Acceptance with real credentials (when Ben has them; spec section 5)**

With the Zoom admin's Meeting SDK app, the Server-to-Server app and one room user: run `.venv/bin/croom --check-zoom -c <a config naming the real credentials file>` on this PC; run the agent with that config and join a meeting hosted on Crystal PM's account from the room page, then one hosted by an outside account (this settles whether a ZAK from the Server-to-Server app satisfies Zoom's rule; if Zoom refuses, the fallback is the SDK app's own OAuth authorization, a follow-up); then a waiting room; then mute, camera and leave from the page. Record the outcome in the ledger. Until the credentials exist this step stays open and does not block the branch review.
