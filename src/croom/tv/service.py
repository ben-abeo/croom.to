"""
The TV service (spec 2026-10-13 TV remote and touchpad, section 3): holds the pairing with the room's LG webOS TV,
keeps one connection to it, and keeps a model of what the TV shows (power, volume, input, app, channel) for the room
page. Every call to the TV goes through an injected client, so the tests touch neither a TV nor the library.
"""

import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from croom.core.config import TvConfig
from croom.core.service import Service
from croom.tv.client import ClientFactory, TvClient
from croom.tv.errors import TvError
from croom.tv.keystore import KeyStore

logger = logging.getLogger(__name__)

Waker = Callable[[str], Awaitable[None]]

NOT_SET_UP = "The TV is not set up: use Find TVs and Pair under Options › TV"
NOT_PAIRED = "Pair the TV under Options › TV"
REFUSED = "The TV no longer accepts this table: Forget TV, then Pair"
PAIRING_BUSY = "Pairing is already waiting for the TV"


def _blank() -> Dict[str, Any]:
    """What the table knows of a TV it has not read."""
    return {"on": False, "reachable": False, "name": "", "volume": None, "muted": False, "input": None,
            "inputs": [], "app": None, "channel": None}


def _why(error: BaseException) -> str:
    """What went wrong, in a few words: the error's own text (a timeout has none)."""
    if isinstance(error, asyncio.TimeoutError):
        return "no answer in time"
    return str(error) or type(error).__name__


def _declined(error: BaseException) -> bool:
    """The library's WebOsTvPairError: the TV showed its prompt and it was declined, or the TV would not register this
    client. Told by the class name (any class in the error's ancestry), so that the library is not imported here."""
    return any(cls.__name__ == "WebOsTvPairError" for cls in type(error).__mro__)


def _first(obj: Any, *names: str) -> Any:
    """The first of these attributes that has something in it, or None."""
    for name in names:
        value = getattr(obj, name, None)
        if value:
            return value
    return None


def _name_of(tv_info: Any) -> str:
    """What to call the TV: the name it gives itself, else its model. The library's WebOsTvInfo keeps the TV's hello
    and system info as `hello` and `system`; the test fake calls them `hello_info` and `system_info`."""
    hello = _first(tv_info, "hello_info", "hello") or {}
    system = _first(tv_info, "system_info", "system") or {}
    return str(hello.get("deviceName") or system.get("modelName") or "")


class TvService(Service):
    RECONNECT_FIRST_S = 2.0   # the wait before trying again at a TV that does not answer; it doubles each time
    RECONNECT_MAX_S = 30.0    # up to this
    WATCH_S = 1.0             # how often a live connection is looked at
    START_WAIT_S = 3.0        # start() waits this long for the first look at the TV, then leaves it to the background

    def __init__(self, config: TvConfig, keystore: KeyStore, client_factory: ClientFactory, waker: Waker,
                 loop_sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        super().__init__("tv")
        self._config, self._keys, self._factory, self._wake, self._sleep = config, keystore, client_factory, waker, loop_sleep
        self._client: Optional[TvClient] = None
        # Not _state: the Service base keeps its lifecycle state there, and the manager sets it
        self._shown: Dict[str, Any] = _blank()
        self._held_off = False
        self._task: Optional[asyncio.Task] = None
        self._started = False
        self._attempt = asyncio.Lock()           # one try at the TV at a time: the loop, a pairing, a wake each make them
        self._first_look = asyncio.Event()
        self._last_failure: Optional[str] = None
        self._pairing = False                    # a pairing is waiting for the person at the TV
        self._refused = False                    # the TV declined the stored key: no more tries until it is paired again
        self._timed_out = False                  # the last try ended in a timeout
        self._letting_go = 0                     # clients this service is hanging up on just now

    @classmethod
    def from_config(cls, config) -> "TvService":
        from croom.tv.client import real_client_factory
        from croom.tv.wake import wake
        return cls(config.tv, KeyStore(config.resolve_data_dir() / "tv-client-key"), real_client_factory, wake)

    @property
    def configured(self) -> bool:
        return bool(self._config.enabled and self._config.host)

    def status(self) -> Dict[str, Any]:
        """What the room page draws from (spec section 6). Reachable is asked now, not as of the last try: a
        connection that has just closed is not reachable, and a TV that cannot be reached is not known to be on."""
        shown = dict(self._shown)
        shown["inputs"] = list(shown["inputs"])
        shown["reachable"] = self._answering()
        shown["on"] = shown["on"] and shown["reachable"]
        return dict(configured=self.configured, paired=self._keys.read() is not None, keep_on=self._config.keep_on,
                    held_off=self._held_off, **shown)

    async def start(self) -> None:
        self._started = True
        self._refused = False                    # a start tries the stored key once more
        if not self.configured:
            logger.info("TV service: no TV is configured")
            return
        if self._keys.read() is None:
            logger.info(f"TV service: the TV at {self._config.host} is not paired; pair it under Options › TV")
            return
        self._first_look = asyncio.Event()
        self._run_loop()
        # The first look gets a moment, so the room page's first status already knows the TV. One that is slow to
        # answer (or off, and not answering at all) is left to the loop: the agent's other services are waiting.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._first_look.wait(), self.START_WAIT_S)
        logger.info("TV service started")

    async def stop(self) -> None:
        self._started = False
        await self._halt_loop()
        await self._drop_client()                # a pairing that waits for the person at the TV ends with the client
        self._shown["reachable"] = self._shown["on"] = False
        logger.info("TV service stopped")

    async def pair(self) -> None:
        """Pair with the TV: connect without a key, so that the TV shows its prompt; the person accepts it with the
        TV's own remote, once. The key the TV hands out is kept and the connection stays.

        The wait for the person is the service's own to cut short: the client is the service's from the moment it is
        built, so stop() and forget() hang up on it, and a caller that goes away leaves nothing connected."""
        if not self.configured:
            raise TvError(NOT_SET_UP)
        if self._pairing:                        # a double tap: one prompt on the TV is enough
            raise TvError(PAIRING_BUSY)
        host = self._config.host
        stopped = TvError(f"Pairing with the TV at {host} was stopped before it finished")
        self._pairing = True
        client: Optional[TvClient] = None
        paired = False
        try:
            await self._halt_loop()
            async with self._attempt:
                await self._drop_client()
                try:
                    client = await self._open(None)
                    self._client = client
                    await self._connected(client)
                except TvError:
                    raise
                except Exception as e:  # noqa: BLE001 - the TV was off, declined, did not answer in time, or was hung up on
                    if self._client is not client:       # hung up on by stop() or forget(): the library answers False
                        raise stopped from e
                    logger.warning(f"Pairing with the TV at {host} failed: {_why(e)}")
                    raise TvError(f"Pairing with the TV at {host} did not finish: check that the TV is on, accept the "
                                  "prompt it shows with its remote, and try again") from e
                if self._client is not client:   # stop() or forget() let go while the TV was waiting for the person
                    raise stopped
                key = client.client_key
                if not key:
                    raise TvError(f"The TV at {host} did not hand out a key: accept the prompt it shows, and try again")
                try:
                    self._keys.write(key)
                except OSError as e:
                    logger.error(f"The TV's key could not be saved to {self._keys.path}: {_why(e)}")
                    raise TvError(f"The TV accepted the pairing but its key could not be kept: the agent needs to be "
                                  f"able to write {self._keys.path}") from e
                self._adopt(client)
                self._refused = False
                paired = True
            logger.info(f"Paired with the TV at {host}")
        finally:
            self._pairing = False
            if client is not None and not paired:
                await self._abandon(client)
            if self._started and not self._refused and self._keys.read() is not None:
                self._run_loop()

    async def forget(self) -> None:
        """Let go of the TV: disconnect and delete the key. The TV keeps its own record of the pairing until it is
        removed in its settings; pairing again replaces it."""
        await self._halt_loop()
        await self._drop_client()                # a pairing that waits for the person at the TV ends with the client
        try:
            try:
                self._keys.clear()
            except OSError as e:
                logger.error(f"The TV's key could not be removed from {self._keys.path}: {_why(e)}")
                raise TvError(f"The TV could not be forgotten: its key could not be removed from {self._keys.path}") from e
            self._shown = _blank()
            self._held_off = False
            logger.info("Forgot the TV")
        finally:
            if self._started and not self._refused and self._keys.read() is not None:
                self._run_loop()                 # the key is still there, so the TV is still wanted

    async def control(self, name: str, value: Any = None) -> Dict[str, Any]:
        """One thing the table asks of the TV; answers with the new status. The controls themselves come with the
        next task: what any control needs first is checked here, and said in words."""
        self._require_ready()
        raise NotImplementedError(f"The TV control {name!r} is not built yet")

    def _require_ready(self) -> TvClient:
        """The client, when the TV can be told something; else why not, in words for the table."""
        if not self.configured:
            raise TvError(NOT_SET_UP)
        if self._keys.read() is None:
            raise TvError(NOT_PAIRED)
        if self._refused:
            raise TvError(REFUSED)
        if self._client is None or not self._answering():
            raise TvError(f"The TV is not answering at {self._config.host}")
        return self._client

    def _answering(self) -> bool:
        """Connected, and read at the last try. The library's is_connected() can raise while a connect that failed is
        being cleaned up (it re-raises that connect's error, or its cancellation, from the future it keeps): the TV is
        not answering then, and the status asked at that moment must not fail. A cancellation raised by a plain call is
        the library's and not this task's: this task cannot be cancelled in the middle of a call that does not wait."""
        client = self._client
        if client is None or not self._shown["reachable"]:
            return False
        try:
            return bool(client.is_connected())
        except (Exception, asyncio.CancelledError):  # noqa: BLE001
            return False

    def _run_loop(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._keep_connected())
            self._task.add_done_callback(self._loop_ended)

    async def _halt_loop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            # asyncio.wait, not await or suppress(): it waits for the task to finish without raising the task's own
            # cancellation here, and a cancel aimed at the caller itself still gets through.
            await asyncio.wait({task})

    @staticmethod
    def _loop_ended(task: asyncio.Task) -> None:
        """Said when the loop's task ends on an error: nobody awaits that task, so nobody else would ever see it."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("TV service: the connection loop ended on an error", exc_info=error)

    async def _keep_connected(self) -> None:
        """Connect, then watch; when the link is lost, connect again. While the TV does not answer the tries back
        off from RECONNECT_FIRST_S to RECONNECT_MAX_S, and start over from the first once it does. It ends when the key
        is gone, and when the TV refuses this table: every try would make the TV show its pairing prompt again."""
        delay = self.RECONNECT_FIRST_S
        try:
            while True:
                try:
                    linked = self._answering()
                    if not linked:
                        if self._keys.read() is None:
                            logger.info("TV service: the key is gone, so there is nothing to connect with")
                            return
                        linked = await self._connect_once()
                        if self._refused:
                            return
                    self._first_look.set()
                    if linked:
                        delay = self.RECONNECT_FIRST_S
                        await self._sleep(self.WATCH_S)
                    else:
                        if self._timed_out:
                            # The library's connect times out when the TV does not answer, and also when it shows its
                            # pairing prompt (it gives the person ten seconds) and nobody accepts it: the two cannot be
                            # told apart. A TV that is off does not mind being tried now and then; one that is made to
                            # show a prompt every few seconds on the wall of a meeting room is a nuisance. So after a
                            # timeout the wait is the longest there is.
                            delay = self.RECONNECT_MAX_S
                        await self._sleep(delay)
                        delay = min(delay * 2, self.RECONNECT_MAX_S)
                except Exception:  # noqa: BLE001 - whatever it was, the TV is not to be left without a loop for good
                    logger.exception("TV service: the connection loop met an error and goes on")
                    self._first_look.set()
                    await self._sleep(delay)
                    delay = min(delay * 2, self.RECONNECT_MAX_S)
        finally:
            self._first_look.set()

    async def _connect_once(self) -> bool:
        """One try at the TV with the stored key: connect and read what it shows. False, with the reason logged once,
        when it does not answer. The one client tries again each time: it keeps its state callback across them."""
        async with self._attempt:
            key = self._keys.read()
            if not self.configured or key is None or self._refused:
                self._set_down()
                return False
            try:
                if self._client is None:
                    self._client = await self._open(key)
                client = self._client
                await self._connected(client)
                self._keep_issued_key(client, key)
                self._adopt(client)
            except Exception as e:  # noqa: BLE001 - refused, timed out, no library: all one thing to the table
                self._set_down()
                self._timed_out = isinstance(e, asyncio.TimeoutError)
                if _declined(e):
                    self._refused = True
                    logger.warning(f"The TV at {self._config.host} no longer accepts this table ({_why(e)}): not trying "
                                   "again until it is forgotten and paired again under Options › TV")
                else:
                    self._note_failure(f"The TV at {self._config.host} is not answering: {_why(e)}")
                return False
            self._timed_out = False
            return True

    def _keep_issued_key(self, client: TvClient, key: str) -> None:
        """A TV that did not know the stored key shows its pairing prompt, and when the person accepts it issues a new
        key. That one is kept, or the next start would make the TV show its prompt again."""
        issued = client.client_key
        if issued and issued != key:
            try:
                self._keys.write(issued)
            except OSError as e:
                logger.error(f"The TV's new key could not be saved to {self._keys.path}: {_why(e)}")
            else:
                logger.info(f"The TV at {self._config.host} issued a new key; it is kept")

    async def _open(self, key: Optional[str]) -> TvClient:
        """A client for the TV with this service's state callback on it, once: the library keeps its callbacks when
        the connection is lost and made again."""
        client = self._factory(self._config.host, key)
        await client.register_state_update_callback(self._on_state)
        return client

    @staticmethod
    async def _connected(client: TvClient) -> None:
        """Connect, or raise. The library raises when the TV does not answer, and answers False when the connection
        was closed while it was being made."""
        if not await client.connect():
            raise ConnectionError("the connection was closed before it was made")

    async def _drop_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await self._close(client)

    async def _abandon(self, client: TvClient) -> None:
        """A client whose pairing did not finish: let go of it if it is still the service's own, and hang up on it
        again if someone else did already (a connection that was not cut short stays up otherwise)."""
        if self._client is client:
            self._client = None
        await self._close(client)

    async def _close(self, client: TvClient) -> None:
        """Hang up on a client. The goodbye it sends (see _on_state) is ours, and no news."""
        self._letting_go += 1
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - it is going away either way
            logger.debug("TV disconnect failed", exc_info=True)
        finally:
            self._letting_go -= 1

    def _adopt(self, client: TvClient) -> None:
        """The client is connected: take what it knows."""
        name = _name_of(client.tv_info)
        if name:
            self._shown["name"] = name
        self._take_state(client.tv_state)
        self._shown["reachable"] = True
        if self._last_failure is not None:
            logger.info(f"The TV at {self._config.host} is answering again")
        self._last_failure = None

    def _set_down(self) -> None:
        self._shown["reachable"] = self._shown["on"] = False

    def _note_failure(self, message: str) -> None:
        """A TV that is off fails every try: warn once for each reason, then quietly."""
        if message != self._last_failure:
            self._last_failure = message
            logger.warning(message)
        else:
            logger.debug(message)

    async def _on_state(self, tv_state: Any) -> None:
        """The library's callback for a change in the TV's state. Nothing may escape it: an error here would end the
        library's handling of the subscription that brought the change."""
        if self._letting_go:
            # We are hanging up: the cleared state the library hands over as a connection ends is our own goodbye, not
            # the TV going off. It changes nothing, and nothing may act on it: the re-wake after an unexpected off (the
            # controls task) belongs below this line.
            return
        try:
            self._take_state(tv_state)
        except Exception:  # noqa: BLE001
            logger.exception("The TV's state could not be read")

    def _take_state(self, tv_state: Any) -> None:
        """Read what the TV shows. A part that is not there reads as unknown, so the cleared state the library hands
        over when the connection ends reads as a TV that is off. The library has no current input or channel number:
        the input is the one whose app is on the screen, and the channel is in the current channel's record. Whether
        the TV is on is the library's own call (`is_on`: Screen Off and Screen Saver are on, the TV is only dark, and
        keeping it on must not wake it), and Active alone when the state comes without one."""
        power = getattr(tv_state, "power_state", None) or {}
        known = getattr(tv_state, "inputs", None) or {}
        app = getattr(tv_state, "current_app_id", None)
        current_input = getattr(tv_state, "current_input", None)
        if current_input is None and app:
            current_input = next((item.get("id") for item in known.values() if item.get("appId") == app), None)
        channel = getattr(tv_state, "channel_number", None)
        if channel is None:
            current = getattr(tv_state, "current_channel", None)
            channel = current.get("channelNumber") if isinstance(current, dict) else None
        is_on = getattr(tv_state, "is_on", None)
        self._shown.update(
            on=bool(is_on) if is_on is not None else power.get("state") == "Active",
            volume=getattr(tv_state, "volume", None),
            muted=bool(getattr(tv_state, "muted", None)),
            input=current_input,
            inputs=[{"id": item.get("id", key), "label": item.get("label") or item.get("id", key)} for key, item in known.items()],
            app=app,
            channel=channel,
        )
