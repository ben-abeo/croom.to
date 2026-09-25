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
