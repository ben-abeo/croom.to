"""
The root helper's socket (spec 2026-10-29, section 5): one JSON request per connection, `{"job": name, ...arguments}`, and one JSON
answer, `{"ok": true, ...}` or `{"ok": false, "error": words}`.

The socket is the only way in, so who may connect is who may ask: it is made owner root, group the room's, mode 660. A job runs in a
thread, off the event loop, so that a slow one (a systemctl restart waits for the agent to stop) does not stop the helper answering.
"""

import asyncio
import grp
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from croom.admin.jobs import JobError

logger = logging.getLogger(__name__)

MAX_REQUEST = 512 * 1024      # a whole config.yaml, or a key file, fits many times over
READ_TIMEOUT_S = 10           # a client that connects and sends nothing does not hold a connection open for good

Job = Callable[[Dict[str, Any]], Dict[str, Any]]


class AdminServer:
    """
    Answers requests on a Unix socket from a table of jobs.

    Args:
        socket_path: where the socket is made; a socket left behind by a helper that crashed is replaced.
        jobs: the job table, name to function (`croom.admin.jobs.Jobs(...).table` on a room device).
        group: the group given the socket, so that the room's user can connect; None leaves the process's own.
    """

    def __init__(self, socket_path, jobs: Dict[str, Job], group: Optional[str] = None):
        self._path = Path(socket_path)
        self._jobs = dict(jobs)
        self._group = group
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        if self._path.exists() or self._path.is_symlink():
            self._path.unlink()
        # Made unreadable to others from the first moment: chmod after the bind would leave a moment with the process's umask.
        old_umask = os.umask(0o117)
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(self._path))
        finally:
            os.umask(old_umask)
        if self._group is not None:
            os.chown(self._path, -1, grp.getgrnam(self._group).gr_gid)
        os.chmod(self._path, 0o660)
        logger.info(f"croom-admin listening on {self._path}")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._path.unlink(missing_ok=True)

    async def serve(self) -> None:
        """Start, and answer requests until cancelled."""
        await self.start()
        try:
            await self._server.serve_forever()
        finally:
            await self.stop()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            answer = await self._answer(reader)
            writer.write(json.dumps(answer).encode("utf-8"))
            await writer.drain()
        except (ConnectionError, OSError) as e:
            logger.warning(f"croom-admin: the client went away: {e}")
        finally:
            writer.close()

    @staticmethod
    async def _read(reader: asyncio.StreamReader) -> bytes:
        """The request: everything until the client closes its side, or one byte more than MAX_REQUEST."""
        raw = b""
        while len(raw) <= MAX_REQUEST:
            more = await reader.read(MAX_REQUEST + 1 - len(raw))
            if not more:
                break
            raw += more
        return raw

    async def _answer(self, reader: asyncio.StreamReader) -> Dict[str, Any]:
        try:
            raw = await asyncio.wait_for(self._read(reader), READ_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "The helper could not read the request: it did not end"}
        if len(raw) > MAX_REQUEST:
            return {"ok": False, "error": "The request is too large"}
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            request = None
        if not isinstance(request, dict) or not isinstance(request.get("job"), str):
            return {"ok": False, "error": "The helper could not read the request"}
        name = request.pop("job")
        job = self._jobs.get(name)
        if job is None:
            logger.warning(f"croom-admin: refused unknown job {name!r}")
            return {"ok": False, "error": "Unknown job"}
        try:
            result = await asyncio.to_thread(job, request)
        except JobError as e:
            logger.info(f"croom-admin: {name}: {e}")
            return {"ok": False, "error": str(e)}
        except Exception as e:
            logger.exception(f"croom-admin: {name} failed")
            return {"ok": False, "error": f"The helper failed: {type(e).__name__}"}
        logger.info(f"croom-admin: {name}: done")
        return {"ok": True, **(result or {})}
