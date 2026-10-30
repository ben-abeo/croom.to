"""
Editing config.yaml from the Settings pages (spec 2026-10-29, section 4).

`/etc/croom/config.yaml` stays the one source of truth. A page's values are read from it as it is now; a save checks each value against
the schema, applies the changes to the file's own mapping (so the settings no page shows are kept as they are), checks the whole, and
sends it to the root helper's `write_config`, which checks it again, keeps the last five versions and renames the new one into place.
The settings the room applies at once (booking hours, camera preset names) are handed to the hooks the control service registers;
the others wait for the room software to restart.
"""

import asyncio
import copy
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import yaml

from croom.core.config import Config
from croom.settings.schema import (BY_PATH, PAGES, SettingError, check_config, get_path, set_path, settings_of, validate)

logger = logging.getLogger(__name__)

Hook = Callable[[Dict[str, Any]], None]
_MISSING = object()


def default_of(path: str) -> Any:
    """A setting's value when the config does not give it: the config's own default."""
    node: Any = Config()
    for part in path.split("."):
        node = getattr(node, part)
    return copy.deepcopy(node)


class ConfigEditor:
    """The Settings pages' hold on config.yaml. `admin` is the root helper's client (croom.admin.client.AdminClient); its AdminError and
    AdminUnavailable pass through `save` for the caller to answer."""

    def __init__(self, config_path, admin):
        self.path = Path(config_path)
        self._admin = admin
        self._hooks: List[Tuple[Tuple[str, ...], Hook]] = []
        self._lock = asyncio.Lock()
        self._pending_restart = False   # a change saved since this process started that applies only after a restart

    @property
    def pending_restart(self) -> bool:
        return self._pending_restart

    def note_restart(self) -> None:
        """Something saved outside the config (a key file) applies after a restart: the pages offer Restart now."""
        self._pending_restart = True

    def on_change(self, paths: Iterable[str], hook: Hook) -> None:
        """Run `hook` after a save that changes any of these live settings, with the values all of them have now ({path: value}): the
        booking hours are handed over together, so that moving both later never passes through an end before the start."""
        self._hooks.append((tuple(paths), hook))

    # --- reading

    def _read(self) -> Dict[str, Any]:
        """The config file's mapping as it is now; {} when there is none. SettingError when it can't be read (a save would lose it)."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise SettingError(f"The room's settings file can't be read: {e.strerror or e}") from e
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise SettingError("The room's settings file is not valid YAML: fix it at the TV Pi") from e
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise SettingError("The room's settings file is not a mapping of sections: fix it at the TV Pi")
        return data

    @staticmethod
    def _value(data: Dict[str, Any], path: str) -> Any:
        """A setting's value in a config mapping, as a page shows it: the file's (tidied as a save would write it), or the default."""
        value = get_path(data, path, None)
        if value is None:
            return default_of(path)
        try:
            return validate(path, value)
        except SettingError:
            return value          # shown as it is, for the person to put right

    def values(self, page: str) -> Dict[str, Any]:
        """A page's settings as the config file has them now. A secret is never read back: it is "" (fields() says whether it is set)."""
        data = self._read()
        return {s.path: ("" if s.kind == "secret" else self._value(data, s.path)) for s in self._page(page)}

    def fields(self, page: str) -> List[Dict[str, Any]]:
        """A page's settings as its form draws them; a secret says whether one is set."""
        data = self._read()
        entries = []
        for s in self._page(page):
            entry = s.describe()
            if s.kind == "secret":
                entry["set"] = bool(get_path(data, s.path, None))
            entries.append(entry)
        return entries

    @staticmethod
    def _page(page: str):
        if page not in PAGES:
            raise KeyError(page)
        return settings_of(page)

    # --- saving

    async def save(self, page: str, changes: Any) -> Dict[str, Any]:
        """Check and write a page's changes ({path: value}). The answer: {"saved": the paths that changed, "restart": whether this save
        waits for a restart, "pending_restart": whether any save since the room started does}. SettingError, with `errors` by path,
        when a value is refused: nothing is written then."""
        allowed = {s.path for s in self._page(page)}
        if not isinstance(changes, dict):
            raise SettingError("Send the settings as a mapping of path to value")
        errors: Dict[str, str] = {}
        checked: Dict[str, Any] = {}
        for path, value in changes.items():
            if path not in allowed:
                errors[str(path)] = "This is not a setting of this page"
                continue
            if BY_PATH[path].kind == "secret" and value in ("", None):
                continue           # the field left blank: the secret stays as it is
            try:
                checked[path] = validate(path, value)
            except SettingError as e:
                errors[path] = str(e)
        if errors:
            raise SettingError(next(iter(errors.values())), errors)

        async with self._lock:
            data = self._read()
            current = copy.deepcopy(data)
            changed = [path for path, value in checked.items() if self._value(data, path) != value]
            if not changed:
                return {"saved": [], "restart": False, "pending_restart": self._pending_restart}
            for path in changed:
                set_path(data, path, checked[path])
            check_config(data, current)
            text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
            await self._admin.call("write_config", yaml=text)
            restart = any(not BY_PATH[path].live for path in changed)
            if not self._apply(data, changed):
                restart = True
        self._pending_restart = self._pending_restart or restart
        logger.info(f"Settings saved: {', '.join(changed)}" + (" (apply after a restart)" if restart else ""))
        return {"saved": changed, "restart": restart, "pending_restart": self._pending_restart}

    def _apply(self, data: Dict[str, Any], changed: List[str]) -> bool:
        """Hand the live settings that changed to their hooks. False when a hook fails: the change is written and waits for a restart."""
        ok = True
        for paths, hook in self._hooks:
            if not any(path in changed for path in paths):
                continue
            try:
                hook({path: self._value(data, path) for path in paths})
            except Exception as e:  # noqa: BLE001 - the file is written: the setting applies at the next start whatever happens here
                logger.warning(f"Could not apply {', '.join(paths)} at once ({type(e).__name__}: {e}): it applies after a restart")
                ok = False
        return ok
