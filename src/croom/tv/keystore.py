"""
Where the TV's client key lives: a private file in the agent's data dir. The key is what the TV handed out at
pairing and lets the agent control it, so it is never in the config or the repo.
"""

import os
from pathlib import Path
from typing import Optional


class KeyStore:
    def __init__(self, path):
        self.path = Path(path)

    def read(self) -> Optional[str]:
        try:
            return self.path.read_text(encoding="utf-8").strip() or None
        except (OSError, ValueError):   # not there, not readable, or not text: there is no key
            return None

    def write(self, key: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Created private: the mode counts for a new file, so the key is never readable by others, not even for an
        # instant. The chmod is for a file that was already there with another mode.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)
        os.chmod(self.path, 0o600)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
