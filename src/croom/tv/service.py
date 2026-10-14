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


def _blank() -> Dict[str, Any]:
    """What the table knows of a TV it has not read."""
    return {"on": False, "reachable": False, "name": "", "volume": None, "muted": False, "input": None,
            "inputs": [], "app": None, "channel": None}


def _why(error: BaseException) -> str:
    """What went wrong, in a few words: the error's own text (a timeout has none)."""
    if isinstance(error, asyncio.TimeoutError):
        return "no answer in time"
    return str(error) or type(error).__name__


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
        await self._drop_client()
        self._shown["reachable"] = self._shown["on"] = False
        logger.info("TV service stopped")

    async def pair(self) -> None:
        """Pair with the TV: connect without a key, so that the TV shows its prompt; the person accepts it with the
        TV's own remote, once. The key the TV hands out is kept and the connection stays."""
        if not self.configured:
            raise TvError(NOT_SET_UP)
        host = self._config.host
        await self._halt_loop()
        try:
            async with self._attempt:
                await self._drop_client()
                client: Optional[TvClient] = None
                try:
                    client = await self._open(None)
                    await self._connected(client)
                except Exception as e:  # noqa: BLE001 - the TV was off, declined, or did not answer in time
                    if client is not None:
                        await self._close(client)
                    logger.warning(f"Pairing with the TV at {host} failed: {_why(e)}")
                    raise TvError(f"Pairing with the TV at {host} did not finish: check that the TV is on, accept the "
                                  "prompt it shows with its remote, and try again") from e
                key = client.client_key
                if not key:
                    await self._close(client)
                    raise TvError(f"The TV at {host} did not hand out a key: accept the prompt it shows, and try again")
                try:
                    self._keys.write(key)
                except OSError as e:
                    await self._close(client)
                    logger.error(f"The TV's key could not be saved to {self._keys.path}: {_why(e)}")
                    raise TvError(f"The TV accepted the pairing but its key could not be kept: the agent needs to be "
                                  f"able to write {self._keys.path}") from e
                self._client = client
                self._adopt(client)
            logger.info(f"Paired with the TV at {host}")
        finally:
            if self._started and self._keys.read() is not None:
                self._run_loop()

    async def forget(self) -> None:
        """Let go of the TV: disconnect and delete the key. The TV keeps its own record of the pairing until it is
        removed in its settings; pairing again replaces it."""
        await self._halt_loop()
        await self._drop_client()
        self._keys.clear()
        self._shown = _blank()
        self._held_off = False
        logger.info("Forgot the TV")

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
        if self._client is None or not self._answering():
            raise TvError(f"The TV is not answering at {self._config.host}")
        return self._client

    def _answering(self) -> bool:
        """Connected, and read at the last try."""
        return self._client is not None and self._shown["reachable"] and bool(self._client.is_connected())

    def _run_loop(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._keep_connected())

    async def _halt_loop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            # asyncio.wait, not await or suppress(): it waits for the task to finish without raising the task's own
            # cancellation here, and a cancel aimed at the caller itself still gets through.
            await asyncio.wait({task})

    async def _keep_connected(self) -> None:
        """Connect, then watch; when the link is lost, connect again. While the TV does not answer the tries back
        off from RECONNECT_FIRST_S to RECONNECT_MAX_S, and start over from the first once it does."""
        delay = self.RECONNECT_FIRST_S
        try:
            while True:
                linked = self._answering()
                if not linked:
                    if self._keys.read() is None:
                        logger.info("TV service: the key is gone, so there is nothing to connect with")
                        return
                    linked = await self._connect_once()
                self._first_look.set()
                if linked:
                    delay = self.RECONNECT_FIRST_S
                    await self._sleep(self.WATCH_S)
                else:
                    await self._sleep(delay)
                    delay = min(delay * 2, self.RECONNECT_MAX_S)
        finally:
            self._first_look.set()

    async def _connect_once(self) -> bool:
        """One try at the TV with the stored key: connect and read what it shows. False, with the reason logged once,
        when it does not answer. The one client tries again each time: it keeps its state callback across them."""
        async with self._attempt:
            key = self._keys.read()
            if not self.configured or key is None:
                self._set_down()
                return False
            try:
                if self._client is None:
                    self._client = await self._open(key)
                await self._connected(self._client)
                self._adopt(self._client)
            except Exception as e:  # noqa: BLE001 - refused, timed out, no library: all one thing to the table
                self._set_down()
                self._note_failure(f"The TV at {self._config.host} is not answering: {_why(e)}")
                return False
            return True

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

    @staticmethod
    async def _close(client: TvClient) -> None:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - it is going away either way
            logger.debug("TV disconnect failed", exc_info=True)

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
        try:
            self._take_state(tv_state)
        except Exception:  # noqa: BLE001
            logger.exception("The TV's state could not be read")

    def _take_state(self, tv_state: Any) -> None:
        """Read what the TV shows. A part that is not there reads as unknown, so the cleared state the library hands
        over when the connection ends reads as a TV that is off. The library has no current input or channel number:
        the input is the one whose app is on the screen, and the channel is in the current channel's record."""
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
        self._shown.update(
            on=power.get("state") == "Active",
            volume=getattr(tv_state, "volume", None),
            muted=bool(getattr(tv_state, "muted", None)),
            input=current_input,
            inputs=[{"id": item.get("id", key), "label": item.get("label") or item.get("id", key)} for key, item in known.items()],
            app=app,
            channel=channel,
        )
