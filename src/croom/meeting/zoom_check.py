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
