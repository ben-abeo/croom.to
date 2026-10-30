"""
The control service's way to the root helper (spec 2026-10-29, section 5): one request per connection to /run/croom-admin.sock.

A machine without the helper (a developer's, the tests) has no socket: `available()` is False and a call raises AdminUnavailable, which
the room page shows as "Not available on this device". A job's refusal is AdminError with the helper's words.
"""

import asyncio
import json
import os
from typing import Any, Dict

SOCKET_PATH = "/run/croom-admin.sock"
NOT_AVAILABLE = "Not available on this device"


class AdminUnavailable(Exception):
    """The helper is not there, or did not answer."""


class AdminError(Exception):
    """A job's refusal or failure, in the helper's words for the person at the screen."""


class AdminClient:
    """
    Args:
        socket_path: the helper's socket.
        timeout: seconds to wait for the whole answer; a restart's systemctl waits for the agent to stop, so the default is generous.
    """

    def __init__(self, socket_path: str = SOCKET_PATH, timeout: float = 120.0):
        self._path = socket_path
        self._timeout = timeout

    def available(self) -> bool:
        return os.path.exists(self._path)

    async def call(self, job: str, **args: Any) -> Dict[str, Any]:
        """The helper's answer to `job` with `args` (`{"ok": True, ...}`); AdminError with its words when it refuses, AdminUnavailable
        when there is no helper to ask or it does not answer."""
        try:
            raw = await asyncio.wait_for(self._exchange(json.dumps({**args, "job": job}).encode("utf-8")), self._timeout)
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise AdminUnavailable(NOT_AVAILABLE) from e
        except asyncio.TimeoutError as e:
            raise AdminUnavailable("The helper did not answer") from e
        except OSError as e:   # no permission on the socket (not the room's user), or the helper went away mid-answer
            raise AdminUnavailable(f"The helper could not be reached: {e.strerror or e}") from e
        try:
            answer = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise AdminUnavailable("The helper's answer could not be read") from e
        if not isinstance(answer, dict):
            raise AdminUnavailable("The helper's answer could not be read")
        if not answer.get("ok"):
            raise AdminError(str(answer.get("error") or "The helper refused"))
        return answer

    async def _exchange(self, request: bytes) -> bytes:
        reader, writer = await asyncio.open_unix_connection(self._path)
        try:
            writer.write(request)
            writer.write_eof()   # the end of the request: the helper reads until it
            await writer.drain()
            return await reader.read()
        finally:
            writer.close()
