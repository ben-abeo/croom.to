"""
The touchpad's transport (spec 2026-10-13 TV remote and touchpad, sections 3 and 5). The events of one /ws/pointer socket move the TV's
cursor, through the pointer socket of the TV's client, or a virtual mouse on the room Pi, through evdev.UInput, which the Pi's desktop takes
like a USB mouse. The TV service and the virtual device are handed in and the libraries are imported where they are used, so the tests run
on fakes: no TV, no evdev and no /dev/uinput.
"""

import asyncio
import errno
import logging
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from croom.tv.errors import TV_NOT_ENABLED, TvError
from croom.tv.names import one_line

logger = logging.getLogger(__name__)

MOUSE_NAME = "Crystal Meet touchpad"
MAX_DELTA = 2000          # the most one message moves or scrolls by: a cursor does not cross a screen twice, so more is a mistake
REASON_LIMIT = 160        # how much of a library's own words goes to the table

TARGETS = ("tv", "pi")
TYPES = ("move", "tap", "right", "scroll", "down", "up")
BAD_MESSAGE = 'A pointer message is a JSON object like {"target": "tv", "type": "move", "dx": 3, "dy": -2}'
BAD_TARGET = 'The target of a pointer message is "tv" or "pi"'
BAD_TYPE = "The type of a pointer message is one of " + ", ".join(TYPES)
KEYS_LATER = "Typing into the room Pi comes in a later version"
NO_RIGHT_BUTTON = "The TV's cursor has no right button"
CLOSED_WHILE_MADE = "The room Pi's mouse was closed while it was being made"

# The kernel's input codes that the virtual mouse uses (linux/input-event-codes.h), by the names evdev.ecodes has for them: here, so
# that this module and its tests need neither evdev nor /dev/uinput.
CODES: Dict[str, int] = {
    "EV_KEY": 0x01, "EV_REL": 0x02,
    "REL_X": 0x00, "REL_Y": 0x01, "REL_WHEEL": 0x08,
    "BTN_LEFT": 0x110, "BTN_RIGHT": 0x111,
}
BUTTONS = {"left": "BTN_LEFT", "right": "BTN_RIGHT"}
# What an error's text says, when it is not the system's own class and number that say it (evdev's UInput looks before it opens)
NOT_ALLOWED_TEXTS = ("permission denied", "cannot be opened for writing")
NOT_THERE_TEXTS = ("no such file", "does not exist", "uinput module")


def _uinput() -> Any:
    """The virtual mouse as evdev makes it: relative motion, a wheel and two buttons. evdev is imported here and not at the top of
    the module, so that a room without a touchpad runs without it."""
    try:
        from evdev import UInput
    except ImportError as e:
        raise TvError("The room Pi's mouse needs the evdev library: pip install evdev") from e
    capabilities = {
        CODES["EV_REL"]: [CODES["REL_X"], CODES["REL_Y"], CODES["REL_WHEEL"]],
        CODES["EV_KEY"]: [CODES["BTN_LEFT"], CODES["BTN_RIGHT"]],
    }
    return UInput(capabilities, name=MOUSE_NAME)


def _why_not(error: BaseException) -> str:
    """Why the virtual mouse could not be made, as words that say what to do. What stands in the way is the device /dev/uinput: the
    user the agent runs as may not be allowed to open it (it is not in the input group, or the device is not the group's), or it is not
    there (the uinput kernel module is not loaded). The installer sets up both. evdev reports a failed open by its own exception as well
    as by the system's, so the words are chosen from the error's number and its text: evdev looks before it opens, and says `"/dev/uinput"
    cannot be opened for writing` or `"/dev/uinput" does not exist or is not a character device file - verify that the uinput module is
    loaded` (its UInput._verify)."""
    text = str(error)
    lowered = text.lower()
    code = getattr(error, "errno", None)
    if isinstance(error, TvError):
        return text
    if isinstance(error, PermissionError) or code == errno.EACCES or any(phrase in lowered for phrase in NOT_ALLOWED_TEXTS):
        return ("The room Pi's mouse is not allowed to use /dev/uinput: the user the agent runs as needs to be in the input group, and "
                "/dev/uinput to belong to it (the installer sets up both; then restart the agent: sudo systemctl restart croom)")
    if isinstance(error, FileNotFoundError) or code == errno.ENOENT or any(phrase in lowered for phrase in NOT_THERE_TEXTS):
        return "The room Pi's mouse needs /dev/uinput, and it is not there: the uinput kernel module is not loaded (sudo modprobe uinput)"
    return (f"The room Pi's mouse could not be made: {one_line(text, REASON_LIMIT) or type(error).__name__} (it needs the uinput kernel "
            "module and the agent's user in the input group, which the installer sets up)")


class PiMouse:
    """A virtual mouse on the room Pi. The device is made when open() is called (the control service calls it when a pointer socket
    opens, in a thread: it takes a moment to appear, and the desktop takes it up a moment after) or, failing that, the first time
    something is sent to it; it is made again after it was lost, and removed by close(). A room that never uses the touchpad has no
    mouse on its desktop. `unavailable` is the reason, in words, when it cannot be made (permission, the module, the library): every
    event then raises TvError with it, and the next one tries again, in case it has been put right, though not sooner than RETRY_S
    after the try that failed.

    uinput_factory makes the device: an object with write(type, code, value) and syn() (and close()), as evdev.UInput has them. The
    default makes the real one. clock is the time (seconds, steadily) that RETRY_S is counted on.
    """

    # close() is called on the event loop's thread (by the agent's stop): it waits this long for a device that is being made, which evdev
    # does in about a second, and then gives up on it. The thread that is making it closes the device when it is made.
    CLOSE_WAIT_S = 2.0
    RETRY_S = 1.0        # a try that failed is not made again within this long: a finger on the pad is an event for every frame

    def __init__(self, uinput_factory: Optional[Callable[[], Any]] = None, clock: Callable[[], float] = time.monotonic):
        self._factory = uinput_factory or _uinput
        self._clock = clock
        self._device: Any = None
        self._making = threading.Lock()     # one thread makes the device: open() is called from the sockets' threads
        self._state = threading.Lock()      # the device and the count below, which close() and a creation that ends must agree on (never held while the device is made)
        self._given_up = 0                  # how many creations close() has given up waiting for: one that began before it closes its own device when it ends
        self._retry_at = 0.0                # the time before which the device is not tried for again (the last try failed)
        self.unavailable: Optional[str] = None

    @property
    def ready(self) -> bool:
        """Whether the device is there: an event is then written to it at once, with nothing to wait for."""
        return self._device is not None

    def open(self) -> None:
        """Make the device now, if it is not there. It takes a moment to appear (evdev waits for it, and looks through /dev/input):
        call this from a thread when the event loop must not wait for it, and ahead of the events when none of them should be the one
        that does. Safe to call from several threads at once, and as often as you like. Raises TvError, in words, as every event does,
        when it cannot be made."""
        self._open()

    def move(self, dx: int, dy: int) -> None:
        """Move the pointer by dx to the right and dy down."""
        self._emit(*[("EV_REL", axis, delta) for axis, delta in (("REL_X", dx), ("REL_Y", dy)) if delta])

    def press(self, button: str = "left") -> None:
        self._emit(("EV_KEY", BUTTONS[button], 1))

    def release(self, button: str = "left") -> None:
        self._emit(("EV_KEY", BUTTONS[button], 0))

    def click(self, button: str = "left") -> None:
        """A press and a release, each reported on its own, as a mouse does (one report with both can be taken as neither)."""
        self.press(button)
        self.release(button)

    def scroll(self, dy: int) -> None:
        """Turn the wheel by dy notches: a negative number is towards the person at the screen (the content moves up, as a
        finger drawn up the screen moves it)."""
        if dy:
            self._emit(("EV_REL", "REL_WHEEL", dy))

    def close(self) -> None:
        """Remove the device (the kernel lets go of a button that was down on it). One that is being made is waited for, for CLOSE_WAIT_S,
        and removed when it is there; if it is not there by then it is given up on, and the thread that is making it closes it when it
        is made (a creation that begins after this call is a use of the mouse again, and is kept)."""
        got = self._making.acquire(timeout=self.CLOSE_WAIT_S)
        with self._state:
            if not got:
                self._given_up += 1
            device, self._device = self._device, None
        if got:
            self._making.release()
        if device is not None:
            self._let_go(device)

    def _open(self) -> Any:
        device = self._device
        if device is not None:
            return device
        with self._making:                             # (the thread that gets here second finds the device made)
            if self._device is not None:
                return self._device
            if self.unavailable is not None and self._clock() < self._retry_at:
                raise TvError(self.unavailable)        # (not tried for again so soon: the same words, and no new try)
            given_up = self._given_up
            try:
                device = self._factory()
            except Exception as e:  # noqa: BLE001 - /dev/uinput cannot be opened, or the library is not there
                reason = _why_not(e)
                if reason != self.unavailable:         # (said once: a page that sends many events a second would say it as often)
                    logger.warning(reason)
                self.unavailable = reason
                self._retry_at = self._clock() + self.RETRY_S
                raise TvError(reason) from e
            with self._state:
                wanted = given_up == self._given_up
                if wanted:
                    self._device = device
            if not wanted:                             # close() did not wait for this one: nobody wants it any more
                self._let_go(device)
                raise TvError(CLOSED_WHILE_MADE)
            self.unavailable = None
            logger.info(f"The room Pi's virtual mouse is ready ({MOUSE_NAME})")
            return device

    def _emit(self, *events: Tuple[str, str, int]) -> None:
        """Write events, by the names of their type and code, and report them as one."""
        if not events:
            return
        device = self._open()
        try:
            for kind, code, value in events:
                device.write(CODES[kind], CODES[code], value)
            device.syn()
        except OSError as e:
            self._lose(device)
            raise TvError(f"The room Pi's mouse stopped taking events: {one_line(str(e), REASON_LIMIT) or type(e).__name__}") from e

    def _lose(self, device: Any) -> None:
        """A device that cannot be written to is let go of, and made again by the next event."""
        if self._device is device:
            self._device = None
        self._let_go(device)

    @staticmethod
    def _let_go(device: Any) -> None:
        try:
            device.close()
        except Exception:  # noqa: BLE001 - it is going away either way
            logger.debug("The room Pi's virtual mouse did not close cleanly", exc_info=True)


def _delta(message: Dict[str, Any], key: str) -> int:
    """How far a message moves or scrolls, along dx or dy: a number, as it was sent (nothing is 0), whole and no more than MAX_DELTA
    either way."""
    value = message.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TvError(f"{key} is a number")
    try:
        whole = int(value)
    except (OverflowError, ValueError) as e:       # Python's JSON parser lets NaN, Infinity and 1e999 through
        raise TvError(f"{key} is a number") from e
    return max(-MAX_DELTA, min(MAX_DELTA, whole))


class PointerRelay:
    """The pointer of one socket: carries its messages out on the TV or on the room Pi (spec section 5, `{"target": "tv"|"pi", "type":
    "move"|"tap"|"right"|"scroll"|"down"|"up", "dx", "dy"}`), and holds what belongs to that socket: a button that is down, the moves
    that wait for the TV. Everything it cannot do or does not understand is a TvError, in words for the table: the socket answers it
    and goes on.

    The TV's moves are coalesced. There is one in flight at most, and what comes in meanwhile is added up and goes in the next, so a TV
    that is slow to take messages is sent the cursor's way and not every step of it. A move is not waited for (the socket goes on reading):
    one that fails on its way is said by the next move, if that comes soon (STALE_FAILURE_S). A tap, a scroll and a down or an up wait
    for the moves before them to go, so the order is kept, and make their own call: they say if the TV is still not taking them, and
    are not refused for what happened to a move. The room Pi's events apply at once.

    The TV is asked for through TvService.pointer(), not through control(): the cursor is not a control (it sends many messages a second,
    it does not wait behind the controls and it reads nothing back). Each message asks once, and uses the client it was given.

    The room Pi's events are applied at once while its device is there. An event that finds none (the socket's own attempt to make it did
    not work, or it was lost) makes it in a thread, off_the_loop(call, *args) being how a thread is waited for (asyncio.to_thread unless the
    socket's owner, which has to be able to end the wait when the service stops, gives its own): the device takes a moment to appear, and
    nothing else on the loop may wait for it.
    """

    CALL_TIMEOUT_S = 2.0      # a message to the TV is only written to its socket: one that takes longer is not being answered
    STALE_FAILURE_S = 1.0     # a move that failed on its way is said by the next move if it comes this soon; after that it is old news

    def __init__(self, tv_service: Any, pi_mouse: PiMouse, off_the_loop: Optional[Callable[..., Awaitable[Any]]] = None):
        self._tv = tv_service
        self._mouse = pi_mouse
        self._off_the_loop = off_the_loop or asyncio.to_thread
        self._pending: Optional[List[int]] = None      # the moves that came in while one was in flight, added up: [dx, dy]
        self._sender: Optional[asyncio.Task] = None    # sends the TV's moves, for as long as there are any to send
        self._failure: Optional[TvError] = None        # what a move that was sent in the background ran into: the next move says it
        self._failed_at = 0.0                          # when (the loop's clock)
        self._client: Any = None                       # the TV's client, as the last move found it: the sender sends with it
        self._pressed = {"tv": False, "pi": False}     # a down that has not had its up, by target
        self._dragged = False                          # the TV's cursor was moved since its down

    @property
    def sending(self) -> Optional[asyncio.Task]:
        """The task that is sending moves to the TV just now, or None: the socket's owner ends it when the service stops."""
        return self._sender if self._sender is not None and not self._sender.done() else None

    async def handle(self, message: Any) -> None:
        """Carry out one message.

        Raises:
            TvError: the message is no use (not an object, an unknown target or type, a delta that is not a number), it cannot be done
                (a right button for the TV, typing), or the TV or the room Pi's mouse cannot be reached. A move for the TV that is sent in the
                background and fails is said by the next move.
        """
        target, kind, dx, dy = self._read(message)
        if kind == "keys":
            raise TvError(KEYS_LATER)
        if target == "pi":
            await self._pi(kind, dx, dy)
        else:
            await self._tv_event(kind, dx, dy)

    async def drain(self) -> None:
        """Wait until nothing is on its way to the TV: the move in flight, and the moves that were added up behind it."""
        sender = self._sender
        if sender is not None:
            await asyncio.wait({sender})

    async def release_all(self) -> None:
        """The socket is over: nothing is left held. A button that is down on the room Pi goes up at once. The move that is in flight
        to the TV finishes, the moves that were waiting for it are dropped, and a drag that was going on is ended there. Raises
        nothing: there is nobody to tell."""
        self._pending = None
        if self._pressed["pi"]:
            self._pressed["pi"] = False
            try:
                if self._mouse.ready:                  # (a device that is gone took the button with it: none is made to let go of it)
                    self._mouse.release("left")
            except Exception:  # noqa: BLE001 - the device is gone, and takes the button with it
                logger.debug("The room Pi's mouse did not take the release of a held button", exc_info=True)
        await self.drain()
        if self._pressed["tv"]:
            self._pressed["tv"] = False
            if self._dragged:
                try:
                    await self._call(self._ready().move, 0, 0)
                except Exception:  # noqa: BLE001 - the TV is gone, and a drag with it
                    logger.debug("The TV did not take the end of a drag", exc_info=True)

    @staticmethod
    def _read(message: Any) -> Tuple[str, str, int, int]:
        if not isinstance(message, dict):
            raise TvError(BAD_MESSAGE)
        target, kind = message.get("target"), message.get("type")
        if not isinstance(target, str) or target not in TARGETS:
            raise TvError(BAD_TARGET)
        if not isinstance(kind, str) or kind not in TYPES + ("keys",):
            raise TvError(BAD_TYPE)
        if kind in ("move", "scroll"):
            return target, kind, _delta(message, "dx"), _delta(message, "dy")
        return target, kind, 0, 0

    async def _pi(self, kind: str, dx: int, dy: int) -> None:
        """An event for the room Pi's mouse: at once, on the loop's thread, when the device is there; when it is not, in a thread, which
        makes it (PiMouse makes the device the first time it is used). An up that finds no device is nothing: the device that is gone
        took the button with it, and a new one is not made for that."""
        if kind == "up":
            held, self._pressed["pi"] = self._pressed["pi"], False
            if held and self._mouse.ready:
                self._mouse.release("left")
        elif self._mouse.ready:
            self._pi_event(kind, dx, dy)
        else:
            await self._off_the_loop(self._pi_event, kind, dx, dy)

    def _pi_event(self, kind: str, dx: int, dy: int) -> None:
        mouse = self._mouse
        if kind == "move":
            mouse.move(dx, dy)
        elif kind == "tap":
            mouse.click("left")
        elif kind == "right":
            mouse.click("right")
        elif kind == "scroll":
            mouse.scroll(dy)
        elif kind == "down":
            mouse.press("left")
            self._pressed["pi"] = True

    async def _tv_event(self, kind: str, dx: int, dy: int) -> None:
        if kind == "right":
            raise TvError(NO_RIGHT_BUTTON)
        if kind == "up":
            await self._lift()
            return
        if kind == "move":
            self._say_what_failed()                    # (a move is the one to say it)
        else:
            self._failure = None                       # a tap, a scroll or a down makes its own call: the TV may be back, and the call says if not
        client = self._ready()                         # once for the message; the TV cannot be told anything: said now, not by the next one
        if kind == "move":
            if dx or dy:
                if self._pressed["tv"]:
                    self._dragged = True
                self._add(dx, dy)
                self._client = client                  # (what the sender sends with, whenever it gets to this move)
                self._send_soon()
            return
        await self._flush()
        if kind == "tap":
            await self._call(client.click)
        elif kind == "scroll":
            if dx or dy:
                await self._call(client.scroll, dx, dy)
        elif kind == "down":                           # nothing is sent: the moves that follow carry it, until the up
            if not self._pressed["tv"]:                # (a down that comes again is the same press: the drag that has begun goes on)
                self._dragged = False
            self._pressed["tv"] = True

    async def _lift(self) -> None:
        """The up of a down. The moves of the drag go first, with the button still down; then the TV is told that it is up: a move
        of nothing, which ends the drag where the finger was lifted. A down and an up with no move between them is a click."""
        if not self._pressed["tv"]:
            return
        try:
            self._failure = None                       # (it makes its own call, as a tap does)
            client = self._ready()
            await self._flush()
        finally:
            self._pressed["tv"] = False                # whatever came of that, nothing is held from here
        if self._dragged:
            await self._call(client.move, 0, 0)
        else:
            await self._call(client.click)

    def _add(self, dx: int, dy: int) -> None:
        pending = self._pending or [0, 0]
        pending[0] = max(-MAX_DELTA, min(MAX_DELTA, pending[0] + dx))
        pending[1] = max(-MAX_DELTA, min(MAX_DELTA, pending[1] + dy))
        self._pending = pending

    def _send_soon(self) -> None:
        """Start sending, when nothing is. The move that is waiting is the first message, as it is now: a burst that comes in before the
        task has begun is added up behind it, and not into it."""
        if self._sender is None or self._sender.done():
            batch, self._pending = self._pending, None
            self._sender = asyncio.ensure_future(self._send_moves(batch))

    async def _send_moves(self, batch: Optional[List[int]]) -> None:
        """Send the moves, one message at a time: what comes in while one is on its way is added up (in _pending), and goes in the next."""
        try:
            while batch is not None:
                dx, dy = batch
                if dx or dy:                           # (moves that cancel out are no move)
                    await self._call(self._client.move, *((dx, dy, 1) if self._pressed["tv"] else (dx, dy)))
                batch, self._pending = self._pending, None
        except TvError as e:                           # nobody awaits this: the next move says it
            self._fail(e)
        except Exception as e:  # noqa: BLE001 - a bug: said by the next move as well, and logged
            logger.warning(f"The TV's cursor could not be moved: {type(e).__name__}: {e}", exc_info=True)
            self._fail(TvError(f"The TV did not accept that: {one_line(str(e), REASON_LIMIT) or type(e).__name__}"))

    def _fail(self, failure: TvError) -> None:
        """The moves that were being sent failed: the ones that waited for them are dropped, and the next move says it."""
        self._pending, self._failure, self._failed_at = None, failure, asyncio.get_running_loop().time()

    async def _flush(self) -> None:
        """Let the moves that are on their way go first. What they ran into is not for the call that waited for them to say: it makes its
        own, and says if the TV is still not taking them."""
        sender = self._sender
        if sender is not None and not sender.done():
            await asyncio.wait({sender})
        self._failure = None

    def _say_what_failed(self) -> None:
        """Said by a move, once, and only while it is news: a move that comes after STALE_FAILURE_S finds the TV as it is now."""
        failure, self._failure = self._failure, None
        if failure is not None and asyncio.get_running_loop().time() - self._failed_at <= self.STALE_FAILURE_S:
            raise failure

    def _ready(self) -> Any:
        """The client for the TV's cursor; TvError, in words, when the TV cannot be told anything."""
        if self._tv is None:
            raise TvError(TV_NOT_ENABLED)
        return self._tv.pointer()

    async def _call(self, call: Callable[..., Any], *args: Any) -> None:
        """One message to the TV's pointer socket, within CALL_TIMEOUT_S; TvError, in words, when it fails."""
        try:
            async with asyncio.timeout(self.CALL_TIMEOUT_S):
                await call(*args)
        except TvError:
            raise
        except TimeoutError:
            raise TvError("The TV is not answering") from None
        except Exception as e:  # noqa: BLE001 - the library's: no connection, a closed socket
            raise TvError(f"The TV did not accept that: {one_line(str(e), REASON_LIMIT) or type(e).__name__}") from e
