"""
The Settings PIN (spec 2026-10-29, sections 2 and 8).

Options › Settings asks for it; the first time, it asks to choose one. The PIN itself is never kept: `settings-pin.json` in the data
directory holds a salted scrypt hash of it (PBKDF2-HMAC-SHA256 where this Python's OpenSSL has no scrypt), mode 600. The file is read
again each time it is needed, so that a forgotten PIN is reset by deleting it at the TV Pi, with no restart.

A right PIN gives a token, which the page sends with each Settings request. Tokens live in memory only (a restart of the room
software locks every screen) and last UNLOCK_SECONDS after the last request that used them; Lock ends one. MAX_TRIES wrong PINs in
a row lock unlocking, for every screen, for LOCKOUT_SECONDS: four digits are ten thousand guesses, and this makes them days.
"""

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

UNLOCK_SECONDS = 5 * 60
MAX_TRIES = 5
LOCKOUT_SECONDS = 10 * 60

PIN_DIGITS = re.compile(r"[0-9]{4,8}")   # plain ASCII digits: str.isdigit() would take other scripts' digits, which no pad can type
BAD_PIN = "The PIN is 4 to 8 digits"
NO_PIN = "Choose a PIN first"
PIN_IS_SET = "A PIN is set already: change it in Settings › Security"
UNREADABLE = "The saved PIN can't be read: delete settings-pin.json in the room's data folder on the TV Pi, then choose a new PIN"

# scrypt's cost: about 16 MB and a twentieth of a second on a laptop, a few times that on a Pi, once per try. What a file says is used
# to check it, within these bounds, so a file written with other costs still checks, and a damaged one cannot ask for gigabytes.
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
PBKDF2_ITERATIONS = 200_000
MAX_SCRYPT_N = 2 ** 17
MAX_PBKDF2_ITERATIONS = 5_000_000


class PinError(Exception):
    """Why the PIN did not do what was asked, in words for the person at the pad; `status` is the route's answer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class PinLocked(PinError):
    """Too many wrong PINs: unlocking is refused for `minutes` more minutes (rounded up, so never 0 while it lasts)."""

    def __init__(self, minutes: int):
        super().__init__(f"Too many wrong PINs: try again in {minutes} minute{'' if minutes == 1 else 's'}", status=423)
        self.minutes = minutes


def _checked(pin: Any) -> str:
    """The PIN as sent, when it is 4 to 8 digits; PinError otherwise. Nothing is trimmed: a PIN pad sends digits and nothing else."""
    if not isinstance(pin, str) or not PIN_DIGITS.fullmatch(pin):
        raise PinError(BAD_PIN)
    return pin


def _hash(pin: str, record: Dict[str, Any]) -> bytes:
    """The hash of `pin` with the salt and cost that `record` names. ValueError for a record that names neither kind, or a cost out of
    bounds."""
    salt = bytes.fromhex(record["salt"])
    if record.get("kdf") == "scrypt":
        n, r, p = int(record["n"]), int(record["r"]), int(record["p"])
        if not (2 <= n <= MAX_SCRYPT_N and 1 <= r <= 16 and 1 <= p <= 4):
            raise ValueError("scrypt cost out of bounds")
        return hashlib.scrypt(pin.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * n * r * p, dklen=32)
    if record.get("kdf") == "pbkdf2_sha256":
        iterations = int(record["iterations"])
        if not 1 <= iterations <= MAX_PBKDF2_ITERATIONS:
            raise ValueError("PBKDF2 cost out of bounds")
        return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations, dklen=32)
    raise ValueError(f"unknown kdf {record.get('kdf')!r}")


def _new_record(pin: str) -> Dict[str, Any]:
    """What the file holds for a new PIN: a fresh salt, the kind and cost of hash, and the hash. Never the PIN."""
    record: Dict[str, Any] = {"version": 1, "salt": secrets.token_hex(16)}
    if hasattr(hashlib, "scrypt"):
        record.update(kdf="scrypt", **SCRYPT)
    else:  # pragma: no cover - a Python built against an OpenSSL without scrypt
        record.update(kdf="pbkdf2_sha256", iterations=PBKDF2_ITERATIONS)
    record["hash"] = _hash(pin, record).hex()
    return record


class PinStore:
    """
    The Settings PIN, its unlock tokens and its count of wrong tries.

    Args:
        path: the PIN file (`settings-pin.json` in the data directory).
        clock: seconds that only go forward (time.monotonic unless a test stands in for it): what tokens and the lock-out are timed by.
    """

    def __init__(self, path, clock: Callable[[], float] = time.monotonic):
        self._path = Path(path)
        self._clock = clock
        self._tokens: Dict[str, float] = {}       # token -> when a request last used it
        self._wrong = 0                           # wrong tries since the last right one or the last lock-out
        self._locked_until = 0.0

    @property
    def path(self) -> Path:
        return self._path

    # --- the PIN

    def is_set(self) -> bool:
        """Whether a PIN has been chosen: whether its file is there. A file that cannot be read counts as a PIN that is set, so that a
        damaged file never lets the next person to open Settings choose a PIN of their own."""
        return self._path.exists()

    def set_pin(self, pin: Any) -> None:
        """Choose the first PIN. PinError when it is not 4 to 8 digits, or when a PIN is set already (change() changes it).

        A PIN is chosen the first time or after a reset at the TV Pi (its file deleted): either way nothing earned against a PIN before it
        stands. Every token, the count of wrong tries and a lock-out are ended, so a reset starts clean and the new PIN is not refused
        for the old one's wrong tries."""
        pin = _checked(pin)
        if self.is_set():
            raise PinError(PIN_IS_SET, status=409)
        self._write(_new_record(pin))
        self._tokens.clear()
        self._wrong = 0
        self._locked_until = 0.0

    def change(self, old: Any, new: Any, keep: Optional[str] = None) -> None:
        """Change the PIN, given the one it is now. The old PIN is tried as unlock() tries one: a wrong one counts towards the lock-out,
        and while it lasts nothing is changed. The new one is checked before the old one is tried, so a mistyped new PIN costs no try.

        Every token but `keep` (the token of the screen that changed it, which goes on working) is ended: a PIN changed after a suspected
        leak must shut out whoever unlocked with the old one, and a token slides, so it could otherwise be kept alive for good."""
        new = _checked(new)
        self._try(old)
        self._write(_new_record(new))
        kept = {keep: self._tokens[keep]} if keep in self._tokens else {}
        self._tokens.clear()
        self._tokens.update(kept)

    # --- unlocking

    def unlock(self, pin: Any) -> str:
        """A new token for the right PIN. PinLocked while unlocking is locked; PinError for no PIN yet, a PIN that is not digits (not
        counted as a try: the pad cannot send one), or a wrong PIN, saying how many tries are left."""
        self._try(pin)
        token = secrets.token_urlsafe(32)
        self._tokens[token] = self._clock()
        return token

    def check(self, token: Optional[str]) -> bool:
        """Whether `token` is a token that is still unlocked; when it is, this request has used it and it lasts UNLOCK_SECONDS from now.
        An expired token is forgotten, so a clock that steps back never brings it back. With no PIN file (a reset at the TV Pi has deleted
        it) no token is unlocked, and every one is ended: what was unlocked with the old PIN goes with it."""
        if not self.is_set():
            self._tokens.clear()
            return False
        now = self._clock()
        for old in [t for t, used in self._tokens.items() if now - used >= UNLOCK_SECONDS]:
            del self._tokens[old]
        if not token or token not in self._tokens:
            return False
        self._tokens[token] = now
        return True

    def lock(self, token: Optional[str]) -> None:
        """End one token (the screen's Lock). A token that has gone already, or was never given, has nothing to end."""
        if token:
            self._tokens.pop(token, None)

    def locked_minutes(self) -> int:
        """The minutes left of a lock-out, rounded up, or 0 when unlocking is open."""
        left = self._locked_until - self._clock()
        return math.ceil(left / 60) if left > 0 else 0

    # --- inside

    def _try(self, pin: Any) -> None:
        """Pass when `pin` is the PIN; raise the PinError that says why not, counting a wrong one and locking after MAX_TRIES.

        The lock-out cannot be raced only because the check, the hash and the count run in one go on the event loop, with no await
        between them: two tries cannot both pass the check before either is counted. Moving the hash off the loop (a thread, for a slow
        Pi) needs an asyncio.Lock held across all of it."""
        minutes = self.locked_minutes()
        if minutes:
            raise PinLocked(minutes)
        pin = _checked(pin)
        record = self._read()
        try:
            right = hmac.compare_digest(_hash(pin, record), bytes.fromhex(record["hash"]))
        except (KeyError, TypeError, ValueError) as e:
            raise PinError(UNREADABLE, status=500) from e
        if right:
            self._wrong = 0
            return
        self._wrong += 1
        if self._wrong >= MAX_TRIES:
            self._wrong = 0
            self._locked_until = self._clock() + LOCKOUT_SECONDS
            raise PinLocked(self.locked_minutes())
        left = MAX_TRIES - self._wrong
        raise PinError(f"Wrong PIN: {left} tr{'y' if left == 1 else 'ies'} left", status=403)

    def _read(self) -> Dict[str, Any]:
        """The file's record. PinError when there is no file (no PIN yet), or one that cannot be read or is not a record."""
        try:
            record = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise PinError(NO_PIN, status=409) from e
        except (OSError, ValueError) as e:
            raise PinError(UNREADABLE, status=500) from e
        if not isinstance(record, dict):
            raise PinError(UNREADABLE, status=500)
        return record

    def _write(self, record: Dict[str, Any]) -> None:
        """Write the record atomically, mode 600 from the start: a half-written file is never what is read, and no other user of the
        machine can read the hash, even for a moment."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=str(self._path.parent), prefix=f".{self._path.name}-")   # mkstemp makes it 600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            os.chmod(temp, 0o600)
            os.replace(temp, self._path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
