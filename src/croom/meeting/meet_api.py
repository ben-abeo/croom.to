"""
The participants of a Google Meet call, from Google's Meet REST API v2 (Meet controls plan Phase B, Task 6; spec 2026-10-30, section 5).

For the organization's own meetings only: the room acts as the call's organizer through the calendar's service account (domain-wide
delegation, scope `meetings.space.readonly`) and asks who is in the call. It is off unless `meeting.meet_api_domain` is set, the organizer has
an address in that domain and the call's meeting code is known. Without the admin's grant, or for another organization's meeting, Google
refuses; that is logged once for the call, not asked again for REFUSED_FOR_S, and the list comes from Meet's page as before.

`MeetParticipants.names(organizer, code)` answers the names of the people in the call now, or None (off, refused, nobody live yet, a
network failure): the caller then keeps what Meet's page says. It never raises.
"""

import asyncio
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/meetings.space.readonly"
BASE_URL = "https://meet.googleapis.com"
CODE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")
REFUSED_FOR_S = 300     # after a refusal nothing is asked for this long
REQUEST_TIMEOUT_S = 10  # one request, or the poll would hang with the call
ONE_READING_S = 60      # and all of a reading's requests together
PAGE_SIZE = 250         # the most the participants list gives in a page
MAX_PAGES = 20          # a call of thousands is not the room's to list (the table shows 50)
NAME_KEYS = ("signedinUser", "anonymousUser", "phoneUser")


class _Refused(Exception):
    """Google, or the sign-in with the room's key, said no: no grant, no such meeting, a key that does not work."""


class _Unavailable(Exception):
    """Nobody to list yet, or Google could not be reached: the page's list serves for now and the next reading tries again."""


def service_account_credentials(path: str, organizer: str):
    """The room's service-account key, acting as `organizer` for the Meet scope (domain-wide delegation)."""
    from google.oauth2 import service_account
    return service_account.Credentials.from_service_account_file(path, scopes=[SCOPE]).with_subject(organizer)


def _refresh_request():
    from google.auth.transport.requests import Request
    return Request()


class MeetParticipants:
    """One call's reader of the participants. `credentials_for(path, organizer)` and `base_url` are for a test's fakes."""

    def __init__(self, domain: str, credentials_path: str, *, base_url: str = BASE_URL,
                 credentials_for: Callable[[str, str], Any] = service_account_credentials, clock: Callable[[], float] = time.monotonic):
        self._domain = (domain or "").strip().lstrip("@").lower()
        self._path = (credentials_path or "").strip()
        self._base_url = base_url.rstrip("/")
        self._credentials_for = credentials_for
        self._clock = clock
        self._session: Optional[aiohttp.ClientSession] = None
        self._credentials: Optional[Any] = None
        self._credentials_organizer = ""
        self._space: Dict[str, str] = {}          # meeting code -> the space's name, which does not change
        self._refused_until = float("-inf")
        self._warned = False                       # a refusal is said once for the call

    def _eligible(self, organizer: str, code: str) -> bool:
        """Whether this call is one to ask about: the room is set up for it, the code is a meeting code and the organizer is in the domain."""
        local, _, host = (organizer or "").strip().lower().partition("@")
        return bool(self._domain and self._path and local and host == self._domain and CODE.match((code or "").lower()))

    async def names(self, organizer: str, code: str) -> Optional[List[str]]:
        if not self._eligible(organizer, code) or self._clock() < self._refused_until:
            return None
        organizer, code = organizer.strip(), code.lower()
        try:
            return await asyncio.wait_for(self._names(organizer, code), ONE_READING_S)
        except _Refused as e:
            self._refused_until = self._clock() + REFUSED_FOR_S
            if not self._warned:
                self._warned = True
                logger.warning(f"Google's Meet API refused the participants list ({e}); it comes from Meet's page "
                               f"(the admin's grant is in the README)")
            else:
                logger.debug(f"Google's Meet API refused the participants list again ({e})")
        except (_Unavailable, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.debug(f"Meet API participants not read ({e or type(e).__name__})")
        except Exception as e:  # noqa: BLE001 - it must never end the call's readings
            logger.debug(f"Meet API participants not read ({type(e).__name__}: {e})")
        return None

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    async def _token(self, organizer: str) -> str:
        if self._credentials is None or self._credentials_organizer != organizer:
            try:
                self._credentials = await asyncio.to_thread(self._credentials_for, self._path, organizer)
            except Exception as e:  # noqa: BLE001 - a key file that is missing or not a service-account key
                raise _Refused(f"the service-account key could not be used: {e}") from None
            self._credentials_organizer = organizer
        credentials = self._credentials
        if not credentials.valid:
            try:
                # The refresh is a blocking HTTP call: off the event loop.
                await asyncio.to_thread(credentials.refresh, _refresh_request())
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ == "TransportError":
                    raise _Unavailable(f"Google's sign-in could not be reached: {e}") from None
                raise _Refused(f"Google would not sign the room in as {organizer}: {e}") from None
        return str(credentials.token)

    async def _get(self, token: str, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S))
        async with self._session.get(self._base_url + path, params=params, headers={"Authorization": f"Bearer {token}"}) as response:
            if response.status == 429 or response.status >= 500:
                raise _Unavailable(f"HTTP {response.status}")
            if response.status != 200:
                raise _Refused(f"HTTP {response.status}")
            body = await response.json(content_type=None)
        if not isinstance(body, dict):
            raise _Unavailable("an answer that is not an object")
        return body

    async def _names(self, organizer: str, code: str) -> List[str]:
        token = await self._token(organizer)
        space = self._space.get(code)
        if space is None:
            space = str((await self._get(token, f"/v2/spaces/{code}")).get("name") or "")
            if not space:
                raise _Refused("no space of that name")
            self._space[code] = space
        conferences = (await self._get(token, "/v2/conferenceRecords",
                                       {"filter": f'space.name="{space}" AND end_time IS NULL'})).get("conferenceRecords") or []
        live = next((c.get("name") for c in conferences if isinstance(c, dict) and c.get("name") and not c.get("endTime")), None)
        if live is None:
            raise _Unavailable("no conference live yet")
        names: List[str] = []
        params: Dict[str, Any] = {"filter": "latest_end_time IS NULL", "pageSize": PAGE_SIZE}
        for _ in range(MAX_PAGES):
            page = await self._get(token, f"/v2/{live}/participants", params)
            for participant in page.get("participants") or []:
                if not isinstance(participant, dict):
                    continue
                name = next((str(participant[key].get("displayName") or "").strip() for key in NAME_KEYS
                             if isinstance(participant.get(key), dict) and participant[key].get("displayName")), "")
                if name:
                    names.append(name)
            if not page.get("nextPageToken"):
                break
            params = {**params, "pageToken": page["nextPageToken"]}
        return names
