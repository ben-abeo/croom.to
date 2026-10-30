"""
The room's key files from the Settings pages (spec 2026-10-29, section 4): the Google service-account key the calendar reads and the Zoom
SDK keys, uploaded from a laptop, read from a USB stick plugged into the TV Pi, or (Zoom) typed.

The root helper checks a key and saves it under /etc/croom for the room's user only (its save_credential job, which uses `summary` here
for the check); the config's path is then set to it through the ConfigEditor, and the room reads it when the room software restarts.
The pages are told what a key is and when it was saved, never what is in it: `describe` reads the file the config names and answers its
summary ("Key for <client_email>") and its time, or what is wrong with it.

This module is imported by the root helper as well: it stays with the standard library and the room's own constants.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from croom.admin.client import AdminError
from croom.meeting.zoom_auth import PLACEHOLDER as ZOOM_PLACEHOLDER
from croom.meeting.zoom_auth import S2S_KEYS as ZOOM_S2S_KEYS
from croom.meeting.zoom_auth import SDK_KEYS as ZOOM_SDK_KEYS
from croom.settings.schema import SettingError

MAX_KEY_FILE = 64 * 1024          # a key file is a few KB
MAX_TYPED = 200                   # a typed SDK key or secret


@dataclass(frozen=True)
class Kind:
    """One kind of key file: its config setting (and that setting's page), and the name the helper saves it as."""
    key: str
    setting: str
    page: str
    file_name: str
    label: str


KINDS: Dict[str, Kind] = {
    "google": Kind("google", "calendar.google_credentials_path", "calendar", "google-service-account.json", "Google service-account key"),
    "zoom": Kind("zoom", "meeting.zoom_credentials_path", "meetings", "zoom-credentials.json", "Zoom SDK keys"),
}


class CredentialError(ValueError):
    """A key refused, or a save that could not be finished, in words for the person at the screen. `status` is the HTTP status the room
    page is answered with."""

    def __init__(self, words: str, status: int = 400):
        super().__init__(words)
        self.status = status


# --- what a key file is


def _blank(data: Dict[str, Any], key: str) -> bool:
    value = data.get(key)
    return not isinstance(value, str) or not value.strip() or value.strip().startswith(ZOOM_PLACEHOLDER)


def what_it_is(data: Dict[str, Any]) -> Optional[str]:
    """What a JSON file uploaded as a key looks like, for a refusal that names it."""
    if data.get("type") == "service_account":
        return "a Google service-account key"
    if any(key in data for key in ZOOM_SDK_KEYS + ZOOM_S2S_KEYS):
        return "a Zoom credentials file"
    if "installed" in data or "web" in data:
        return "a Google OAuth client file"
    return None


def _google_summary(data: Dict[str, Any]) -> str:
    if data.get("type") != "service_account":
        what = what_it_is(data)
        if what:
            raise CredentialError(f"This is {what}, not a Google service-account key")
        raise CredentialError('This is not a Google service-account key: it has no "type": "service_account"')
    for key in ("client_email", "private_key"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CredentialError(f"This Google key has no {key}: download a new key for the service account")
    return f"Key for {data['client_email'].strip()}"


def _zoom_summary(data: Dict[str, Any]) -> str:
    if data.get("type") == "service_account":
        raise CredentialError("This is a Google service-account key, not Zoom credentials")
    for key in ZOOM_SDK_KEYS:
        if _blank(data, key):
            raise CredentialError(f"These Zoom credentials have no {key} (or it is still {ZOOM_PLACEHOLDER})")
    present = [key for key in ZOOM_S2S_KEYS if not _blank(data, key)]
    if present and len(present) != len(ZOOM_S2S_KEYS):
        missing = ", ".join(key for key in ZOOM_S2S_KEYS if key not in present)
        raise CredentialError(f"These Zoom credentials have no {missing}: the server-to-server values and room_user go together")
    if present:
        return f"Zoom Meeting SDK keys, joining as {data['room_user'].strip()}"
    return "Zoom Meeting SDK keys"


def summary(kind: str, data: Any) -> str:
    """What a key file of `kind` is, in words that hold nothing secret ("Key for room@….iam.gserviceaccount.com"); CredentialError,
    naming what it is instead, when it is not that kind of key."""
    if kind not in KINDS:
        raise CredentialError("The key must be a Google service-account key or Zoom credentials")
    if not isinstance(data, dict):
        raise CredentialError("This is not a JSON file: a key file is the .json file the console downloads")
    return _google_summary(data) if kind == "google" else _zoom_summary(data)


def parse(content: str) -> Dict[str, Any]:
    """A key file's text as its JSON object; CredentialError when it is not one."""
    try:
        data = json.loads(content)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise CredentialError("This is not a JSON file: a key file is the .json file the console downloads")
    return data


def typed_zoom(key: Any, secret: Any, current: Optional[Dict[str, Any]] = None) -> str:
    """The Zoom credentials file for an SDK key and secret typed at the page. The server-to-server values of the file the room has now
    (`current`) are kept: typing the SDK keys does not take away the room's own Zoom user."""
    errors = {}
    values = {}
    for name, value, label in (("key", key, "The SDK key"), ("secret", secret, "The SDK secret")):
        text = value.strip() if isinstance(value, str) else ""
        if not text:
            errors[name] = f"{label} can't be blank"
        elif len(text) > MAX_TYPED or any(ch.isspace() for ch in text):
            errors[name] = f"{label} is one word of at most {MAX_TYPED} characters, as the Zoom app's page shows it"
        values[name] = text
    if errors:
        raise SettingError(next(iter(errors.values())), errors)
    data: Dict[str, Any] = {"sdk_client_id": values["key"], "sdk_client_secret": values["secret"]}
    if isinstance(current, dict) and all(not _blank(current, name) for name in ZOOM_S2S_KEYS):
        data.update({name: current[name].strip() for name in ZOOM_S2S_KEYS})
    return json.dumps(data, indent=2) + "\n"


def read_key(path: str) -> Optional[Dict[str, Any]]:
    """The JSON object in a key file, or None when it can't be read as one (no file, not JSON, too large)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_KEY_FILE + 1)
    except OSError:
        return None
    if len(raw) > MAX_KEY_FILE:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def describe(kind: str, path: Any) -> Optional[Dict[str, Any]]:
    """What the key file at `path` is, for the page: {"summary", "saved_at", "path"}, or {"path", "problem"} when it is not a key the
    room can use (the problem in words, for the person to upload it again); None when the config names no file."""
    if not isinstance(path, str) or not path.strip():
        return None
    path = path.strip()
    if not os.path.isabs(path):
        return {"path": path, "problem": f"{path} is not a file's full path: upload the key again"}
    try:
        saved = os.stat(path).st_mtime
    except FileNotFoundError:
        return {"path": path, "problem": f"There is no file at {path}: upload the key again"}
    except OSError as e:
        return {"path": path, "problem": f"{path} can't be read ({e.strerror or e}): upload the key again"}
    data = read_key(path)
    if data is None:
        return {"path": path, "problem": f"{path} can't be read as a key file: upload the key again"}
    try:
        words = summary(kind, data)
    except CredentialError as e:
        return {"path": path, "problem": f"{e}: upload the key again"}
    return {"summary": words, "saved_at": datetime.fromtimestamp(saved).astimezone().isoformat(timespec="seconds"), "path": path}


# --- the pages' way to the helper


class Credentials:
    """The Settings pages' key files. `editor` is the ConfigEditor (the config's path to a key is set through it), `admin` the root
    helper's client: its AdminUnavailable passes through for the caller to answer "Not available on this device"."""

    def __init__(self, editor, admin):
        self._editor = editor
        self._admin = admin

    @staticmethod
    def kind(key: str) -> Kind:
        """The kind named `key`; KeyError when there is no such kind."""
        return KINDS[key]

    def _path_of(self, kind: Kind) -> Any:
        return self._editor.values(kind.page).get(kind.setting)

    def status(self) -> Dict[str, Optional[Dict[str, Any]]]:
        """{"google": what its file is, or None, "zoom": …}: never what is in them. SettingError when the config can't be read."""
        return {key: describe(key, self._path_of(kind)) for key, kind in KINDS.items()}

    async def usb_files(self) -> Dict[str, Any]:
        answer = await self._admin.call("usb_files")
        return {"files": answer.get("files") or []}

    async def save(self, key: str, content: str) -> Dict[str, Any]:
        """Hand a key file's text to the helper, which checks and saves it; then point the config at it. The answer says what was
        saved ({"summary", "saved_at", "path"}) and that it applies after a restart. The helper's refusal passes through (AdminError)."""
        kind = self.kind(key)
        answer = await self._admin.call("save_credential", kind=key, content=content)
        path = str(answer.get("path") or "")
        try:
            await self._editor.save(kind.page, {kind.setting: path})
        except (SettingError, AdminError) as e:
            raise CredentialError(f"The key is saved, but the room's settings could not be pointed at it: {e}", 409) from e
        self._editor.note_restart()   # a new key in the same file is read at the next start as well
        saved = describe(key, path) or {}
        return {
            "kind": key,
            "summary": saved.get("summary") or answer.get("summary") or "",
            "saved_at": saved.get("saved_at") or datetime.now().astimezone().isoformat(timespec="seconds"),
            "path": path,
            "restart": True,
            "pending_restart": self._editor.pending_restart,
        }

    async def save_from_usb(self, key: str, path: Any) -> Dict[str, Any]:
        """The key file at `path` on a USB stick (one usb_files listed): read by the helper, then saved as an upload is."""
        self.kind(key)
        if not isinstance(path, str) or not path:
            raise CredentialError("Choose a key file on the USB stick")
        answer = await self._admin.call("read_usb_file", path=path)
        return await self.save(key, str(answer.get("content") or ""))

    async def save_typed_zoom(self, key: Any, secret: Any) -> Dict[str, Any]:
        """The Zoom SDK key and secret typed at the page, saved as the Zoom credentials file (keeping the room's own Zoom user)."""
        path = self._path_of(KINDS["zoom"])
        current = read_key(path.strip()) if isinstance(path, str) and os.path.isabs(path.strip()) else None
        return await self.save("zoom", typed_zoom(key, secret, current))
