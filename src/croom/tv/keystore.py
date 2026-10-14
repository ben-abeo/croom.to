"""
Where the TV's client key lives: a private file in the agent's data dir. The key is what the TV handed out at
pairing and lets the agent control it, so it is never in the config or the repo.
"""

import contextlib
import os
from pathlib import Path
from typing import Optional


class KeyStore:
    def __init__(self, path):
        self.path = Path(path)

    @property
    def _temp(self) -> Path:
        return self.path.with_name(self.path.name + ".tmp")

    def read(self) -> Optional[str]:
        try:
            return self.path.read_text(encoding="utf-8").strip() or None
        except (OSError, ValueError):   # not there, not readable, or not text: there is no key
            return None

    def write(self, key: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The key is written whole to a file of its own, flushed to the disk, and renamed over the old one: a power cut
        # leaves the old key or the new one, never an empty file. That file is created 0600 (the mode counts for a new
        # file; fchmod is for one a crash left behind), so the key is never readable by others, not even for an
        # instant, and the rename puts it in place of whatever mode the old key file had.
        temp = self._temp
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(key)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                temp.unlink()
            raise

    def clear(self) -> None:
        for path in (self.path, self._temp):    # a file a crash left behind holds a key too
            try:
                path.unlink()
            except FileNotFoundError:
                pass
