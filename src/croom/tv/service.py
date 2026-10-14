"""
The TV service (spec 2026-10-13 TV remote and touchpad, section 3): holds the pairing with the room's LG webOS TV,
keeps one connection to it, and keeps a model of what the TV shows (power, volume, input, app, channel) for the room
page. It carries out what the table asks of the TV, wakes the TV with Wake-on-LAN, and keeps it on unless the table
turned it off. Every call to the TV goes through an injected client, and the wake through an injected waker, so the
tests touch neither a TV nor the libraries.
"""

import asyncio
import contextlib
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from croom.core.config import TvConfig
from croom.core.service import Service
from croom.tv.client import ClientFactory, TvClient
from croom.tv.errors import TvError, TvTimeout
from croom.tv.keystore import KeyStore
from croom.tv.names import one_line

logger = logging.getLogger(__name__)

Waker = Callable[[str], Awaitable[None]]

NOT_SET_UP = "The TV is not set up: use Find TVs and Pair under Options › TV"
NOT_PAIRED = "Pair the TV under Options › TV"
REFUSED = "The TV no longer accepts this table: Forget TV, then Pair"
PAIRING_BUSY = "Pairing is already waiting for the TV"
NOBODY_ACCEPTED = "Nobody accepted the prompt on the TV within {seconds:g} seconds: tap Pair to try again"

# The remote's keys, by the names aiowebostv has for them (its buttons module; spec section 2). The library does not check
# a name, and a TV that is sent one it does not know ignores it without a word, so a name outside this set is refused here.
KEYS = frozenset(
    "LEFT RIGHT UP DOWN ENTER BACK EXIT HOME MENU QMENU INFO GUIDE LIST RECENT MYAPPS INPUT_HUB MUTE VOLUMEUP VOLUMEDOWN "
    "CHANNELUP CHANNELDOWN PLAY PAUSE STOP REWIND FASTFORWARD RECORD RED GREEN YELLOW BLUE 0 1 2 3 4 5 6 7 8 9 DASH "
    "ASTERISK CC AD SAP ASPECT_RATIO SCREEN_REMOTE TELETEXT TEXTOPTION 3D_MODE LIVE_ZOOM MAGNIFIER_ZOOM PROGRAM AMAZON "
    "NETFLIX POWER".split()
)


def _blank() -> Dict[str, Any]:
    """What the table knows of a TV it has not read."""
    return {"on": False, "reachable": False, "name": "", "volume": None, "muted": False, "input": None,
            "inputs": [], "app": None, "channel": None}


def _why(error: BaseException) -> str:
    """What went wrong, in a few words: the error's own text (a timeout has none)."""
    if isinstance(error, asyncio.TimeoutError):
        return "no answer in time"
    return str(error) or type(error).__name__


_ERROR_TEXT = re.compile(r"""errorText['"]?:\s*(?:'([^']*)'|"([^"]*)")""")


def _reason(error: BaseException) -> str:
    """Why the TV did not carry out a command, as a line for the table. The library's message quotes the TV's whole answer
    (`Invalid request response {'type': 'response', 'payload': {'returnValue': False, 'errorText': 'invalid channel'}}`):
    the reason is the TV's own errorText when it is there, else the message, cut short."""
    text = _why(error)
    found = _ERROR_TEXT.search(text)
    reason = (found.group(1) or found.group(2)) if found else ""
    text = reason or text
    return text if len(text) <= 160 else text[:159] + "…"


def _whole(value: Any, low: int, high: int, what: str) -> int:
    """A whole number in a range, as the table sent it: a number, or the digits of one."""
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            pass
    elif isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise TvError(f"{what} is a whole number from {low} to {high}")
    return value


def _text(value: Any, what: str) -> str:
    """Words the table typed or chose: a string with something in it."""
    if not isinstance(value, str) or not value.strip():
        raise TvError(f"{what} is needed")
    return value.strip()


def _channel_number(value: Any) -> str:
    """A channel as it is typed: its digits, and a sub-channel after a dash or a dot (7-1)."""
    text = str(value).strip() if isinstance(value, (str, int)) else ""
    if not re.fullmatch(r"\d{1,5}([-.]\d{1,3})?", text):
        raise TvError("A channel is a number, like 12")
    return text


def _input_of(tv_state: Any, app: Optional[str]) -> Optional[str]:
    """The input whose app is on the screen. The library has no current input: its inputs are keyed by the id of the app
    each one runs, and the input is the one whose app is on. (The test fake names its current input itself.)"""
    current_input = getattr(tv_state, "current_input", None)
    if current_input is None and app:
        known = getattr(tv_state, "inputs", None) or {}
        current_input = next((item.get("id") for item in known.values() if item.get("appId") == app), None)
    return current_input


def _app_of(input_id: str) -> str:
    """The app webOS runs for an HDMI input, by its naming: HDMI_1 is com.webos.app.hdmi1."""
    return "com.webos.app." + input_id.lower().replace("_", "")


def shows_the_pi(pi_input: str, shown_input: Optional[str], app: Optional[str]) -> Optional[bool]:
    """Whether the TV shows the input the room Pi is on (tv.pi_input), by the input and the app it has on its screen; None
    when that cannot be told: no input is named for the room Pi, or the TV says neither an input nor an app. An input the TV's
    list names is compared by its id. When the list cannot be matched to the app (there is an app, and no input for it), the
    app's own name answers: a TV running com.webos.app.hdmi1 is on HDMI_1, so a room Pi that is on HDMI_1 needs no switching.
    Live TV and the other apps are not the room Pi."""
    if not pi_input or (shown_input is None and not app):
        return None
    if shown_input is not None:
        return shown_input == pi_input
    return app == _app_of(pi_input)


def _power_says_on(power_state: Any) -> Optional[bool]:
    """Whether a power state, as the TV reports it, is on; None when it says nothing. The library's rule: a TV is on in every
    state but standby, suspended and off (Screen Off and Screen Saver are on: the TV is only dark)."""
    if not isinstance(power_state, dict) or not power_state.get("state"):
        return None
    return power_state["state"] not in ("Power Off", "Suspend", "Active Standby", "Off")


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
    and system info as `hello` and `system`; the test fake calls them `hello_info` and `system_info`. The TV chooses its name, and
    it is shown on the table and printed on a terminal: one printable line (names.one_line), and a name with nothing printable
    in it is no name: the model stands in."""
    hello = _first(tv_info, "hello_info", "hello") or {}
    system = _first(tv_info, "system_info", "system") or {}
    return one_line(hello.get("deviceName")) or one_line(system.get("modelName"))


def _line(value: Any) -> Any:
    """What the TV calls one of its things (an input, the app on its screen, a channel), when it is words: one printable line,
    as its name is. It reaches the table's page and a terminal, and is the TV's to choose. Anything else (nothing, a number)
    stays what it is: only words are tidied. `<` and `&` stay too: the page shows these as text."""
    return one_line(value) if isinstance(value, str) else value


def _listed(key: str, item: Dict[str, Any]) -> Dict[str, str]:
    """An input of the TV's list as the table shows it: its id, and the label the TV gives it (the id, when the label is
    nothing worth showing), each on one line. The id is the one `input` and the list agree on: the same rule is applied to both."""
    ident = _line(item.get("id", key))
    return {"id": ident, "label": _line(item.get("label")) or ident}


class TvService(Service):
    RECONNECT_FIRST_S = 2.0   # the wait before trying again at a TV that does not answer; it doubles each time
    RECONNECT_MAX_S = 30.0    # up to this
    WATCH_S = 1.0             # how often a live connection is looked at
    START_WAIT_S = 0.5        # start() waits this long for the first look at the TV, then leaves it to the background
    REWAKE_AFTER_S = 5.0      # a TV that went off by itself is woken after this long, unless it is back on by then
    WAKE_TIMEOUT_S = 15.0     # a wake waits this long for the TV to answer (a join waits no longer for it, whatever is slow)
    WAKE_POLL_S = 1.0         # and looks at the TV this often meanwhile
    WAKE_RESEND_S = 3.0       # sending the magic packet again this often while it does not (one can be lost)
    TURN_ON_GRACE_S = 0.5     # a TV that answers in standby is told to turn on, and has this long to say that it has
    # A person's tap keeps its own command and grace beyond the wait, so a Turn on from the table takes at most 15 (the wait,
    # WAKE_TIMEOUT_S) + 3 (the command, ASK_TIMEOUT_S) + 3 (the grace, below) + 2 (the read-back, REREAD_TIMEOUT_S) = 23 s
    MANUAL_TURN_ON_GRACE_S = 3.0   # when a person at the table asked for it, this long: they are waiting to be told
    ASK_TIMEOUT_S = 3.0       # a command that the service gives the TV on its own account (turn on, show the Pi) has this long
    REREAD_TIMEOUT_S = 2.0    # and each read-back after a control has this long
    # A pairing waits this long for the person at the TV to accept the prompt. The library's own wait for it (ten seconds) does not
    # end: aiohttp restarts its receive timeout on every ping and pong of the connection, and the library pings every five seconds.
    PAIR_WAIT_S = 60.0
    # A TV that has accepted by then (it has handed the client its key) is given this long more to finish connecting: the library goes
    # on after the key to open its input socket and read the TV's state, and a pairing cut off there leaves the TV with an entry for a
    # client whose key was never kept, and the person told that nobody accepted.
    PAIR_FINISH_S = 5.0

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
        self._rewake: Optional[asyncio.Task] = None   # the wake that is waiting its turn: one at a time
        self._controls = {                       # what the table can ask of the TV, each by the name the control API gives it
            "volume_up": self._volume_up, "volume_down": self._volume_down, "volume": self._volume, "mute": self._mute,
            "channel_up": self._channel_up, "channel_down": self._channel_down, "channel": self._channel,
            "input": self._input, "app": self._app, "key": self._key, "digit": self._digit, "toast": self._toast,
        }

    @classmethod
    def from_config(cls, config) -> "TvService":
        from croom.tv.client import real_client_factory
        from croom.tv.wake import wake
        return cls(config.tv, KeyStore(config.resolve_data_dir() / "tv-client-key"), real_client_factory, wake)

    @property
    def configured(self) -> bool:
        return bool(self._config.enabled and self._config.host)

    @property
    def key_path(self) -> Path:
        """Where the key the TV handed out is kept."""
        return self._keys.path

    def status(self) -> Dict[str, Any]:
        """What the room page draws from (spec section 6). Reachable is asked now, not as of the last try: a
        connection that has just closed is not reachable, and a TV that cannot be reached is not known to be on. Refused
        says that the TV declined the key it was paired with, which the person puts right (Forget TV, then Pair) and
        which a TV that is only off or away is not: the page tells the two apart by it. (Read it with `paired`: it
        stays set after Forget TV, until the next pairing or start.)"""
        shown = dict(self._shown)
        shown["inputs"] = list(shown["inputs"])
        shown["reachable"] = self._answering()
        shown["on"] = shown["on"] and shown["reachable"]
        return dict(configured=self.configured, paired=self._keys.read() is not None, keep_on=self._config.keep_on,
                    held_off=self._held_off, refused=self._refused, **shown)

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
        # A TV that is not on as the agent starts is woken. That is the background's business: the wake waits for the first
        # look at the TV by itself (and does nothing for a TV that answers), and the agent does not wait for it.
        if self._wants_on():
            self._wake_after(0)
        # The first look gets a moment, so the room page's first status already knows the TV. One that is slow to
        # answer (or off, and not answering at all) is left to the loop: the agent's other services are waiting.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._first_look.wait(), self.START_WAIT_S)
        logger.info("TV service started")

    async def stop(self) -> None:
        self._started = False
        await self._end(self._take_wake())       # a wake that is waiting its turn goes with the service
        await self._halt_loop()
        await self._drop_client()                # a pairing that waits for the person at the TV ends with the client
        self._shown["reachable"] = self._shown["on"] = False
        logger.info("TV service stopped")

    async def pair(self) -> None:
        """Pair with the TV: connect without a key, so that the TV shows its prompt; the person accepts it with the
        TV's own remote, once. The key the TV hands out is kept and the connection stays.

        The wait for the person is the service's own to cut short: the client is the service's from the moment it is
        built, so stop() and forget() hang up on it, and a caller that goes away leaves nothing connected. It is bounded, too:
        a prompt nobody accepts within PAIR_WAIT_S ends the pairing, in words (the library's own wait for it does not end), and
        the next tap on Pair starts another. A TV that has accepted by then is given PAIR_FINISH_S more to finish connecting."""
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
                waiting: Optional[asyncio.Timeout] = None
                try:
                    client = await self._open(None)
                    self._client = client
                    # The timeout is the person's wait and the TV's time to finish: at the end of the first the pairing nobody has
                    # accepted is cut off (_wait_is_up), and one the TV has accepted goes on to the end of the second
                    async with asyncio.timeout(self.PAIR_WAIT_S + self.PAIR_FINISH_S) as waiting:
                        due = asyncio.get_running_loop().call_later(self.PAIR_WAIT_S, self._wait_is_up, waiting, client)
                        try:
                            await self._connected(client)
                        finally:
                            due.cancel()
                except TvError:
                    raise
                except Exception as e:  # noqa: BLE001 - the TV was off, declined, did not answer in time, or was hung up on
                    if self._client is not client:       # hung up on by stop() or forget(): the library answers False
                        raise stopped from e
                    if waiting is not None and waiting.expired() and not client.client_key:   # this wait, and not the library's own timeout
                        logger.warning(f"Pairing with the TV at {host}: nobody accepted the prompt within {self.PAIR_WAIT_S:g} s")
                        raise TvError(NOBODY_ACCEPTED.format(seconds=self.PAIR_WAIT_S)) from e
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
        await self._end(self._take_wake())
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

    async def probe(self, timeout: float) -> None:
        """One look at the TV with the key it was paired with, for `croom --check-tv`: returns once the TV has answered (status()
        then says what it shows), and raises TvError, in words, when it cannot be asked or does not answer, the look being cut
        off after `timeout` seconds (a TvTimeout, as is a look that ended in the library's own timeout). A look is no more than
        that: the service is not started, nothing is woken, and a service with no key is not connected at all, because a
        connect without a key is a pairing. (A TV that did not know the key shows its prompt, and if somebody accepts it during
        the look the TV issues a new key, which is kept, as at every connect: the one change a look can make.) Hang up with
        stop()."""
        self._require_paired()
        try:
            answered = await asyncio.wait_for(self._connect_once(), timeout)
        except asyncio.TimeoutError:
            raise TvTimeout(f"The TV at {self._config.host} did not answer within {timeout:g} seconds") from None
        if answered:
            return
        if self._refused:
            raise TvError(REFUSED)
        words = self._last_failure or f"The TV at {self._config.host} is not answering"
        if self._timed_out:                      # the library's own timeout: the same as the look's own, for the one who reads it
            raise TvTimeout(words)
        raise TvError(words)

    async def control(self, name: str, value: Any = None) -> Dict[str, Any]:
        """One thing the table asks of the TV; answers with the status as the TV shows it now. What the TV cannot do
        raises TvError, in words for the table: the control API answers 409 with them.

        What stands in the way is said first, in this order: no TV set up, not paired, a TV that no longer accepts this
        table, and (for every control but turning the TV on, which is how it is reached) a TV that does not answer."""
        self._require_paired()
        if name == "key" and isinstance(value, str) and value.strip().upper() == "POWER":
            # The remote's power key switches the TV. From the table that is the table turning it off or on, and it holds
            # as the Power control does, or the TV would be woken again a moment after it was switched off.
            name, value = "power", "off" if self._is_on() else "on"
        if name == "power":
            await self._set_power(value)
            return self.status()
        action = self._controls.get(name) if isinstance(name, str) else None
        if action is None:
            raise TvError(f"The TV has no control called {name!r}")
        client = self._require_ready()
        try:
            await action(client, value)
        except TvError:
            raise
        except Exception as e:  # noqa: BLE001 - the TV did not carry it out: refused, no answer in time, or the link is gone
            logger.warning(f"The TV at {self._config.host} did not accept {name}: {_why(e)}")
            raise TvError(f"The TV did not accept that: {_reason(e)}") from e
        await self._read_back(client, name)
        return self.status()

    async def _set_power(self, value: Any) -> None:
        if value == "on":
            self._held_off = False               # the person wants the TV on: nothing holds it off any more
            self._cancel_wake()                  # and a wake that was waiting its turn is this one now
            if not await self.ensure_on(manual=True) and not self._answering():
                raise TvError(f"The TV at {self._config.host} did not turn on: check that Wake-on-LAN is enabled on it "
                              "(Mobile TV On) and that tv.mac is its MAC address")
            client = self._client
            if client is not None and self._answering():
                await self._read_back(client, "power")
            if not self._is_on():
                # The TV answers, was told to turn on (a refusal is the same), and is still not on. The person at the table
                # is told so: a Turn on that answered "off" without a word would be the table lying by saying nothing.
                raise TvError(f"The TV at {self._config.host} answered but did not turn on: press its power button")
        elif value == "off":
            client = self._require_ready()
            self._cancel_wake()
            # Held before the TV is told: it may say that it is off before this command has been answered, and that is
            # the table's doing, not a TV to wake again (_on_state)
            self._held_off = True
            try:
                await client.power_off()
            except Exception as e:  # noqa: BLE001 - not sent: the TV is still on, and nothing holds it off
                self._held_off = False
                logger.warning(f"The TV at {self._config.host} did not accept power off: {_why(e)}")
                raise TvError(f"The TV did not accept that: {_reason(e)}") from e
            self._shown["on"] = False
        else:
            raise TvError("Power is on or off")

    async def _volume_up(self, client: TvClient, value: Any) -> None:
        await client.volume_up()

    async def _volume_down(self, client: TvClient, value: Any) -> None:
        await client.volume_down()

    async def _volume(self, client: TvClient, value: Any) -> None:
        await client.set_volume(_whole(value, 0, 100, "The volume"))

    async def _mute(self, client: TvClient, value: Any) -> None:
        """Mute, unmute, or (with no value) switch from what the table shows. The TV tells of the change too, but a
        second tap that comes before it does must switch back, so the answer to the command is taken as it comes."""
        if value is None:
            muted = not self._shown["muted"]
        elif isinstance(value, bool):
            muted = value
        else:
            raise TvError("Mute is true or false, or nothing to switch it")
        await client.set_mute(muted)
        self._shown["muted"] = muted

    async def _channel_up(self, client: TvClient, value: Any) -> None:
        await client.channel_up()

    async def _channel_down(self, client: TvClient, value: Any) -> None:
        await client.channel_down()

    async def _channel(self, client: TvClient, value: Any) -> None:
        """The TV opens a channel by its id, and has the number only as one of the channel's fields: a number typed at the
        table is looked up in the channel list the TV keeps (a room with no tuner has none), and sent as typed when it is
        not there."""
        number = _channel_number(value)
        channel_id = number
        for channel in getattr(client.tv_state, "channels", None) or ():
            if isinstance(channel, dict) and str(channel.get("channelNumber")) == number and channel.get("channelId"):
                channel_id = channel["channelId"]
                break
        await client.set_channel(channel_id)

    async def _input(self, client: TvClient, value: Any) -> None:
        """An input of the TV, by the id in its list; "pi" is the input the room Pi is on (tv.pi_input in the config)."""
        pi = isinstance(value, str) and value.strip() == "pi"
        input_id = self._config.pi_input if pi else _text(value, "An input")
        known = [item["id"] for item in self._shown["inputs"]]
        if known and input_id not in known:      # the TV lists its inputs: one it does not list is no use to ask for
            if pi:
                raise TvError(f"The TV has no input {input_id!r}, which is where the config says the room Pi is (tv.pi_input)")
            raise TvError(f"The TV has no input called {input_id!r}")
        await client.set_input(input_id)

    async def _app(self, client: TvClient, value: Any) -> None:
        await client.launch_app(_text(value, "An app"))

    async def _key(self, client: TvClient, value: Any) -> None:
        name = _text(value, "A remote key").upper()
        if name not in KEYS:
            raise TvError(f"The TV's remote has no key called {name!r}")
        await client.button(name)

    async def _digit(self, client: TvClient, value: Any) -> None:
        await client.button(str(_whole(value, 0, 9, "A digit")))

    async def _toast(self, client: TvClient, value: Any) -> None:
        await client.send_message(_text(value, "A message"))

    async def _read_back(self, client: TvClient, name: str, deadline: Optional[float] = None) -> None:
        """Read back what the TV shows, as the table is waiting to see it: the TV tells of every change too, but not before
        the control has been answered. The volume and the mute are read after every control (any of them may have touched
        them), and the field the control changed besides: the input and the app, the channel, the power. The reads go
        together, each is given REREAD_TIMEOUT_S (or what is left of `deadline`, a time on the loop's clock, when that is
        less: with none left nothing is read), and one the TV does not answer leaves what is shown. Nothing is read after
        turning the TV off: it is going away, and what it would say now is what it said before it went."""
        limit = self._within(self.REREAD_TIMEOUT_S, deadline)
        if not limit:
            return
        reads = {"volume": client.get_volume, "muted": client.get_muted}
        if name == "input":
            reads["app"] = client.get_input             # (the library's current input is the id of the app that is running)
        elif name == "app":
            reads["app"] = client.get_current_app
        elif name in ("channel", "channel_up", "channel_down"):
            reads["channel"] = client.get_current_channel
        elif name == "power":
            reads["power"] = client.get_power_state
        answers = await asyncio.gather(*(self._read(ask, limit) for ask in reads.values()), return_exceptions=True)
        for field, answer in zip(reads, answers):
            if isinstance(answer, BaseException):
                logger.debug(f"The TV's {field} could not be read back: {_why(answer)}")
                continue
            self._take_read(client, field, answer)

    @staticmethod
    async def _read(ask: Callable[[], Awaitable[Any]], limit: float) -> Any:
        return await asyncio.wait_for(ask(), limit)

    def _take_read(self, client: TvClient, field: str, answer: Any) -> None:
        """What one read-back said, when it said something this service can use."""
        if field == "volume" and isinstance(answer, int) and not isinstance(answer, bool):
            self._shown["volume"] = answer
        elif field == "muted" and isinstance(answer, bool):
            self._shown["muted"] = answer
        elif field == "app" and isinstance(answer, str) and answer:
            self._shown["app"] = _line(answer)
            self._shown["input"] = _line(_input_of(client.tv_state, answer))
        elif field == "channel" and isinstance(answer, dict) and answer.get("channelNumber"):
            self._shown["channel"] = _line(answer["channelNumber"])
        elif field == "power":
            on = _power_says_on(answer)
            if on is not None:
                self._shown["on"] = on

    def _require_paired(self) -> None:
        """Why the TV cannot be told anything at all, in words; nothing when it is set up, paired and accepts this table."""
        if not self.configured:
            raise TvError(NOT_SET_UP)
        if self._keys.read() is None:
            raise TvError(NOT_PAIRED)
        if self._refused:
            raise TvError(REFUSED)

    def _require_ready(self) -> TvClient:
        """The client, when the TV can be told something; else why not, in words for the table."""
        self._require_paired()
        if self._client is None or not self._answering():
            raise TvError(f"The TV is not answering at {self._config.host}")
        return self._client

    def pointer(self) -> TvClient:
        """The client for the TV's cursor (the pointer socket of the library's client): the live connection when the TV can be told
        something, else why not, as TvError in the words control() has for it. The cursor is not one of the controls. It sends many
        messages a second, so it takes no turn behind them (the control service's lock) and reads nothing back; the pointer relay
        (croom.tv.pointer) calls the client's move, click and scroll itself."""
        return self._require_ready()

    # Keeping the TV on (spec section 3): woken as the agent starts, before every join, and after it went off by itself;
    # never against the table, which holds it off from a manual off until a manual on or the next join.

    async def ensure_on(self, timeout: Optional[float] = None, manual: bool = False) -> bool:
        """Wake the TV and wait for it to answer (up to `timeout`, WAKE_TIMEOUT_S by default); whether it answered. It is
        then on, or has been told to turn on and given a moment to say so.

        A TV that is on is left alone, and one that the table turned off stays off, unless a person asked for it (`manual`).
        The wake is a magic packet, sent again every WAKE_RESEND_S while there is no answer; what comes after is looking, not
        hoping: the TV is asked (`_connect_once`) until it answers, and no try goes on past the time that is left. A TV that
        went off the network but not off arrives here as a cleared state, the same as one that went off, so finding it
        answering again is as good as having woken it. One that keeps its socket up in standby (Quick Start+) answers at
        once and is not on: the packet may not reach it (Mobile TV On is off), so it is told to turn on over its socket, as
        the remote would. A wake that cannot be sent is the person's to hear of (TvError); for a join or a wake of the
        service's own it is logged, and the TV is not waited for.

        All of it is within `timeout`, the packet and the steps after the TV has answered (telling it to turn on, the moment it
        is given to say that it has) included, except a person's tap (`manual`), whose command and grace come after the wait:
        they are waiting to be told, and a TV that answered in the last moments is still given its time. A join, which waits
        no longer than that for the TV, never takes longer."""
        if self._is_on():
            return True
        if not self._still_wanted(manual):
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.WAKE_TIMEOUT_S if timeout is None else timeout)
        try:
            await self._wake(self._config.mac)
        except TvError as e:
            if manual:
                raise
            logger.warning(f"The TV cannot be woken: {e}")
            return False
        logger.info(f"Sent Wake-on-LAN to the TV ({self._config.mac}); waiting for it to answer")
        sent = loop.time()
        while True:
            if not self._can_wait():                 # stopped, or the TV can no longer be asked, while it was being waited for
                return False
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                # The library's connect takes two seconds for a TV that does not answer and ten for one that is slow to say
                # hello: a try that is still going when the time is up is cut off (it lets go of its client, see there)
                answered = await asyncio.wait_for(self._connect_once(), remaining)
            except asyncio.TimeoutError:
                return False
            if answered:
                if not self._is_on():
                    await self._turn_on(manual, None if manual else deadline)
                return True
            if loop.time() - sent >= self.WAKE_RESEND_S:      # still nothing: a packet can be lost, or sent too soon
                sent = loop.time()
                try:
                    await self._wake(self._config.mac)
                except TvError as e:
                    logger.warning(f"The TV cannot be woken again: {e}")
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(self.WAKE_POLL_S, remaining))

    async def _turn_on(self, manual: bool = False, deadline: Optional[float] = None) -> None:
        """The TV answers on its socket and is not on (standby with the network kept up): tell it to turn on, as the remote
        would, and give it a moment to say that it has (a few seconds when a person at the table is waiting). A TV that
        refuses is logged and not waited for. Neither the command nor the moment goes on past `deadline` (a time on the
        loop's clock; a person's tap has none): with none of it left, the TV is not asked."""
        client = self._client
        ask = self._within(self.ASK_TIMEOUT_S, deadline)
        if client is None or not ask:
            return
        try:
            await asyncio.wait_for(client.power_on(), ask)
        except Exception as e:  # noqa: BLE001 - not a reason to fail a wake that found the TV
            logger.warning(f"The TV at {self._config.host} did not accept power on: {_why(e)}")
            return
        loop = asyncio.get_running_loop()
        moment = loop.time() + (self.MANUAL_TURN_ON_GRACE_S if manual else self.TURN_ON_GRACE_S)
        if deadline is not None:
            moment = min(moment, deadline)
        while not self._is_on() and loop.time() < moment:
            await asyncio.sleep(min(0.05, moment - loop.time()))

    @staticmethod
    def _within(limit: float, deadline: Optional[float]) -> float:
        """How long a step that is given `limit` seconds may take, when it has to be over by `deadline` (a time on the loop's
        clock; None: it has no deadline): the limit, or what is left of the time when that is less; 0 when none is left."""
        if deadline is None:
            return limit
        return max(0.0, min(limit, deadline - asyncio.get_running_loop().time()))

    async def before_join(self) -> bool:
        """A meeting is about to be joined (the meeting service calls this first): the TV is wanted on, whatever the table
        turned it off for, and the join waits up to WAKE_TIMEOUT_S for it to answer; a TV that answers and is on is switched to
        the room Pi's input if it is not showing it. All of that is within WAKE_TIMEOUT_S: the join waits no longer than that
        for the TV. Whether the TV answered: the join goes on either way. A TV that there is nothing to wait for (not set up,
        not paired, not accepting this table, a service that is stopped) is not one that failed to come on: the answer is True,
        and the room is not warned about it before every join."""
        if not self._config.keep_on:
            return True
        self._held_off = False
        if not self._can_wait():
            logger.debug("TV service: nothing to wake before the join (the TV is not set up, not paired, has refused this "
                         "table, or the service is stopped)")
            return True
        deadline = asyncio.get_running_loop().time() + self.WAKE_TIMEOUT_S
        answered = await self.ensure_on()
        if answered and self._is_on():
            await self._show_the_pi(deadline)
        return answered

    async def _show_the_pi(self, deadline: Optional[float] = None) -> None:
        """A meeting is not joined onto a TV that is not showing the room Pi: a TV that is not on the input the room Pi is
        on (another input, live TV, an app) is switched to it. Only a TV whose state is not known (it says neither an input
        nor an app) is left alone, and one the input list cannot be matched for is on the room Pi when the app it runs is the
        Pi's (shows_the_pi). A TV that refuses is logged and the join goes on. The switch and the reads after it are held to
        `deadline` (a time on the loop's clock), as the steps of the wake are."""
        pi, shown, app, client = self._config.pi_input, self._shown["input"], self._shown["app"], self._client
        ask = self._within(self.ASK_TIMEOUT_S, deadline)
        if client is None or shows_the_pi(pi, shown, app) is not False or not ask:
            return
        try:
            await asyncio.wait_for(client.set_input(pi), ask)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"The TV at {self._config.host} did not switch to the room Pi's input {pi}: {_why(e)}")
            return
        logger.info(f"The TV at {self._config.host} showed {shown or app}: switched to the room Pi's input {pi}")
        await self._read_back(client, "input", deadline)

    def _is_on(self) -> bool:
        return bool(self._shown["on"]) and self._answering()

    def _can_connect(self) -> bool:
        """The TV is set up, paired, and has not refused this table: it can be asked, and so waited for."""
        return self.configured and not self._refused and self._keys.read() is not None

    def _can_wait(self) -> bool:
        """The service runs and the TV can be asked: a wake can go on."""
        return self._started and self._can_connect()

    def _still_wanted(self, manual: bool) -> bool:
        """Whether the TV is to be woken: it can be, and nothing at the table says off (a person who asks for it on is
        the table)."""
        return self._can_wait() and (manual or not self._held_off)

    def _wants_on(self) -> bool:
        """Whether the TV is to be kept on just now: asked for in the config, the service running, and not turned off at
        the table."""
        return self._config.keep_on and self._still_wanted(False)

    def _wake_after(self, delay: float) -> None:
        """Wake the TV in a little while, if it is not on by then. One such wait at a time: a TV that goes off twice in
        that time is woken once."""
        if self._rewake is None or self._rewake.done():
            self._rewake = asyncio.create_task(self._wake_when_due(delay))

    async def _wake_when_due(self, delay: float) -> None:
        try:
            if delay:
                await asyncio.sleep(delay)       # (none: as the service starts, the look at the TV decides at once)
            await self._first_look_over()
            await self.ensure_on()               # which does nothing for a TV that is back on, or was turned off at the table
        except Exception:  # noqa: BLE001 - nobody awaits this task: say what went wrong, and let the next off try again
            logger.exception("TV service: waking the TV failed")

    async def _first_look_over(self) -> None:
        """A wake that was scheduled as the service started does not wait for the first look at the TV where start() does:
        it waits here, in the background (a TV that answers needs no waking)."""
        if self._task is not None and not self._task.done():
            await self._first_look.wait()

    def _cancel_wake(self) -> None:
        """The table decided: a wake that is waiting its turn is not wanted (or is the one being asked for)."""
        if self._rewake is not None and not self._rewake.done():
            self._rewake.cancel()

    def _take_wake(self) -> Optional[asyncio.Task]:
        task, self._rewake = self._rewake, None
        return task

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
        await self._end(task)

    @staticmethod
    async def _end(task: Optional[asyncio.Task]) -> None:
        """End a background task and wait until it is over."""
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
            client = None
            try:
                if self._client is None:
                    self._client = await self._open(key)
                client = self._client
                held = client.client_key         # what the client holds going in: only a change during the connect is the TV's
                await self._connected(client)
                self._keep_issued_key(client, held)
                self._adopt(client)
            except asyncio.CancelledError:
                # Whoever awaited this try was cancelled (a join given up on, a wake ended, a request dropped). The library's
                # connect() then leaves its client with a cancelled future, which its connect task keeps until it ends: every
                # connect() and is_connected() on that client raises CancelledError meanwhile, out of the next try, whoever
                # makes it. So the client is let go of, and hung up on even if we are cancelled again while we do it.
                if client is not None:
                    if self._client is client:
                        self._client = None
                    await asyncio.shield(self._close(client))
                raise
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

    def _keep_issued_key(self, client: TvClient, held: Optional[str]) -> None:
        """A TV that did not know the stored key shows its pairing prompt, and when the person accepts it issues a new
        key. That one is kept, or the next start would make the TV show its prompt again. Only a key the TV changed during
        this connect: the client holds the key it was built with, and the file may have been edited by hand since, which
        is no news from the TV and no reason to write the old key over the edit."""
        issued = client.client_key
        if issued and issued != held:
            try:
                self._keys.write(issued)
            except OSError as e:
                logger.error(f"The TV's new key could not be saved to {self._keys.path}: {_why(e)}")
            else:
                logger.info(f"The TV at {self._config.host} issued a new key; it is kept")

    async def _open(self, key: Optional[str]) -> TvClient:
        """A client for the TV with this service's state callback on it, once: the library keeps its callbacks when
        the connection is lost and made again. The callback belongs to its client: one that the service has let go of (a
        hang-up that failed, and says goodbye later) is no longer the TV, and what it says counts for nothing."""
        client = self._factory(self._config.host, key)

        async def on_state(tv_state: Any) -> None:
            if client is self._client:
                await self._on_state(tv_state)
        await client.register_state_update_callback(on_state)
        return client

    @staticmethod
    async def _connected(client: TvClient) -> None:
        """Connect, or raise. The library raises when the TV does not answer, and answers False when the connection
        was closed while it was being made."""
        if not await client.connect():
            raise ConnectionError("the connection was closed before it was made")

    @staticmethod
    def _wait_is_up(waiting: asyncio.Timeout, client: TvClient) -> None:
        """The person's wait for the prompt is over (PAIR_WAIT_S). A pairing that nobody has accepted ends now. One that the TV has
        accepted (the client holds the key the TV handed out) is left to finish, in the PAIR_FINISH_S that the timeout has left."""
        if not client.client_key:
            waiting.reschedule(asyncio.get_running_loop().time())

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
            # the TV going off. It changes nothing, and nothing may act on it: the re-wake after an unexpected off, below
            # this line, must not take it for one.
            return
        was_on = self._shown["on"]
        try:
            self._take_state(tv_state)
        except Exception:  # noqa: BLE001
            logger.exception("The TV's state could not be read")
            return
        if was_on and not self._shown["on"] and self._wants_on():
            # Off by itself, or by its own remote (a TV whose network dropped says the same): nobody at the table did it
            # (that holds), so it is woken in a moment, if it is not back on by then.
            self._wake_after(self.REWAKE_AFTER_S)

    def _take_state(self, tv_state: Any) -> None:
        """Read what the TV shows. A part that is not there reads as unknown, so the cleared state the library hands
        over when the connection ends reads as a TV that is off. The library has no current input or channel number:
        the input is the one whose app is on the screen, and the channel is in the current channel's record. Whether
        the TV is on is the library's own call (`is_on`: Screen Off and Screen Saver are on, the TV is only dark, and
        keeping it on must not wake it), and Active alone when the state comes without one."""
        power = getattr(tv_state, "power_state", None) or {}
        known = getattr(tv_state, "inputs", None) or {}
        app = getattr(tv_state, "current_app_id", None)
        current_input = _input_of(tv_state, app)
        channel = getattr(tv_state, "channel_number", None)
        if channel is None:
            current = getattr(tv_state, "current_channel", None)
            channel = current.get("channelNumber") if isinstance(current, dict) else None
        is_on = getattr(tv_state, "is_on", None)
        self._shown.update(
            on=bool(is_on) if is_on is not None else power.get("state") == "Active",
            volume=getattr(tv_state, "volume", None),
            muted=bool(getattr(tv_state, "muted", None)),
            input=_line(current_input),
            inputs=[_listed(key, item) for key, item in known.items()],
            app=_line(app),
            channel=_line(channel),
        )
