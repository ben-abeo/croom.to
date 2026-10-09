"""
Small settings kept next to the agent's state: the screensaver choice and the
camera's home and presets. One JSON file, written atomically, readable by the
service user only (spec 2026-10-08 sound and camera, section 4.5).
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict


class SettingsStore:
    def __init__(self, path):
        self._path = Path(path)
        self._data: Dict[str, Any] = self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Dict[str, Any]:
        """The file's contents; a missing, unreadable or broken file is simply empty."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def save(self, key: str, value: Any) -> None:
        """Set one key and write the whole file atomically, mode 600.

        A value that cannot be serialised, or a write that fails, raises before anything changes:
        the value is remembered only once it is on disk, so a later save of another key cannot
        carry a value the caller was told did not save.
        """
        text = json.dumps({**self._data, key: value})
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=str(self._path.parent), prefix=f".{self._path.name}-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(temp, 0o600)
            os.replace(temp, self._path)
            self._data[key] = value
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
