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
        assert "Zoom Meeting SDK: app sdkClient123, signature minted (Zoom checks it only when a meeting is joined)" in text
        assert "Zoom account: server-to-server token obtained for account acct789" in text
        assert "Zoom room user: room1@crystalpm.com, ZAK obtained (valid 2 hours)" in text
        assert text.rstrip().endswith("Ready: this room should join meetings hosted by any Zoom account; the first outside-hosted meeting proves it.")
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
