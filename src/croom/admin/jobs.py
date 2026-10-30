"""
The root helper's jobs (spec 2026-10-29, sections 4 and 5): the fixed list of privileged things the room page may ask for, each checking
its arguments, none running a shell.

Everything a job touches on the machine comes in through `Host`: the folders (`Paths`), the command runner, the owner the room's files
are given, the probe of the room's status page, the clock and the background thread. On a room device they are the real ones; the tests
put a temporary folder and stand-ins in their place, so nothing there needs root.

A job takes the request's arguments (a dict) and gives the answer's fields (a dict), or raises JobError with words for the person at
the screen, which the server sends back as they are.
"""

import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from croom.core.config import Config
from croom.settings.credentials import KINDS as CREDENTIAL_KINDS
from croom.settings.credentials import CredentialError
from croom.settings.credentials import parse as parse_credential
from croom.settings.credentials import summary as credential_summary
from croom.settings.schema import SettingError, check_config

logger = logging.getLogger(__name__)

KEEP_VERSIONS = 5                 # config.yaml.1 ... config.yaml.5
HEALTH_TIMEOUT_S = 2 * 60         # how long a restarted room has to answer GET /api/status
HEALTH_INTERVAL_S = 5             # how often it is asked
PROBE_TIMEOUT_S = 4               # one ask: under the interval, so a hanging room does not stretch the watch
UNDO_WINDOW_S = 10 * 60           # a change written this long before a restart is the change the room may not start with
DEFAULT_CONTROL_PORT = 8080
LOOPBACK = "127.0.0.1"
ANY_ADDRESS = ("", "0.0.0.0", "::")   # a room page listening on every address is asked on the loopback
RESTARTING = "The room is restarting: try again in a moment"
REBOOT_DELAY_S = 1.0              # the answer is on its way to the room page before the machine goes down
MAX_USB_FILE = 64 * 1024          # a key file is a few KB; anything bigger on a stick is not one
MAX_CREDENTIAL = 64 * 1024
MAX_CONFIG = 256 * 1024
COMMAND_TIMEOUT_S = 90            # systemctl restart waits for the agent to stop, which can take a while

CREDENTIAL_FILES = {key: kind.file_name for key, kind in CREDENTIAL_KINDS.items()}

Runner = Callable[[List[str]], Tuple[int, str]]


class JobError(Exception):
    """A job's refusal or failure, in words for the person at the screen: the server answers with them as they are."""


def run_command(args: List[str]) -> Tuple[int, str]:
    """Run a command (an argument list: never a shell): (exit code, its output, or its errors when it failed). A timeout is code 124
    and a missing command 127; neither raises."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]} timed out after {COMMAND_TIMEOUT_S} s"
    except OSError as e:
        return 127, str(e)
    return done.returncode, done.stdout if done.returncode == 0 else (done.stderr or done.stdout)


def status_answers(host: str, port: int) -> bool:
    """Whether the room's agent answers GET /api/status at the address its room page listens on: the sign that it started with its
    config."""
    address = f"[{host}]" if ":" in host else host
    try:
        with urllib.request.urlopen(f"http://{address}:{port}/api/status", timeout=PROBE_TIMEOUT_S) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):   # refused, timed out, an HTTP error status (HTTPError is an OSError), a bad port
        return False


def in_background(work: Callable[[], None]) -> None:
    """Run `work` in a daemon thread: the restart job answers at once and the watch goes on after it."""
    threading.Thread(target=work, name="croom-admin-watch", daemon=True).start()


@dataclass
class Paths:
    """The folders the jobs work in: the real ones by default, a temporary folder in the tests."""
    config_dir: Path = Path("/etc/croom")
    data_dir: Path = Path("/var/lib/croom")
    media_root: Path = Path("/media")
    install_dir: Path = Path("/opt/croom")

    def __post_init__(self):
        for name in ("config_dir", "data_dir", "media_root", "install_dir"):
            setattr(self, name, Path(getattr(self, name)))

    @property
    def config(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def state(self) -> Path:
        return self.data_dir / "admin-state.json"

    @property
    def version_file(self) -> Path:
        return self.install_dir / "VERSION"


@dataclass
class Host:
    """What the jobs touch on the machine. `owner` and `group` are who the room software runs as: its config and keys are theirs."""
    paths: Paths = field(default_factory=Paths)
    run: Runner = run_command
    owner: str = "croom"
    group: str = "croom"
    probe: Callable[[str, int], bool] = status_answers
    clock: Callable[[], float] = time.time       # wall-clock seconds: the state file's times outlive a reboot
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic   # the watch's 2 minutes: a clock set while it waits does not stretch or end them
    background: Callable[[Callable[[], None]], None] = in_background


# --- checks


def _only(args: Dict[str, Any], *names: str) -> None:
    """Refuse an argument the job does not take: the helper does what its list says and nothing more."""
    extra = sorted(str(key) for key in args if key not in names)
    if extra:
        raise JobError(f"This job does not take {', '.join(extra)}")


def _text(args: Dict[str, Any], name: str, limit: int) -> str:
    value = args.get(name)
    if not isinstance(value, str):
        raise JobError(f"The {name} must be text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as e:   # a lone surrogate from JSON's \ud800: it cannot be written as a file's text
        raise JobError(f"The {name} must be text") from e
    if size > limit:
        raise JobError(f"The {name} is too large")
    return value


def check_config_yaml(text: Any, current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The settings in `text` as a mapping, when the room would start with them; JobError saying why not otherwise.

    The text must be YAML, a mapping of sections, and load with Config.from_dict (a key a section does not know, or a section that is not
    a mapping, stops the agent: load_config would set the whole file aside); and every setting the settings schema knows must be within
    its range (croom.settings.schema.check_config). `current` is the config on disk now, when there is one: the room page may not be
    turned off, nor moved from its port or address, which would lock Settings out of the room."""
    if not isinstance(text, str):
        raise JobError("The settings must be text")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        where = getattr(e, "problem_mark", None)
        line = f" (line {where.line + 1})" if where is not None else ""
        raise JobError(f"The settings are not valid YAML{line}") from e
    if not isinstance(data, dict):
        raise JobError("The settings must be a mapping of sections (room:, meeting:, ...)")
    try:
        Config.from_dict(data)
    except (TypeError, ValueError, AttributeError, KeyError) as e:
        raise JobError(f"The room would not start with these settings: {e}") from e
    try:
        check_config(data, current)
    except SettingError as e:
        raise JobError(str(e)) from e
    return data


# --- the jobs


class Jobs:
    """The helper's jobs on one machine. `table` is the list the server answers from: a name it does not hold is an unknown job."""

    def __init__(self, host: Optional[Host] = None):
        self._host = host or Host()
        self._write_lock = threading.Lock()     # one config write (or undo) at a time: the rotation is several renames
        self._watch_lock = threading.Lock()
        self._watching = False
        self.table: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
            "write_config": self.write_config,
            "save_credential": self.save_credential,
            "usb_files": self.usb_files,
            "read_usb_file": self.read_usb_file,
            "restart": self.restart,
            "restart_status": self.restart_status,
            "reboot": self.reboot,
            "version": self.version,
        }

    @property
    def _paths(self) -> Paths:
        return self._host.paths

    # --- writing files

    def _place(self, target: Path, content: bytes, mode: int) -> None:
        """Write `content` beside `target` and rename it into place: the room never reads half a file. The temporary file is made with
        its final owner and mode before a byte of a key goes into it, and is removed when anything fails."""
        fd, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        try:
            os.fchmod(fd, mode)
            shutil.chown(temp, user=self._host.owner, group=self._host.group)
            with os.fdopen(fd, "wb") as f:
                fd = -1
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, target)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
            raise

    def _rotate(self) -> None:
        """Move the last versions along (config.yaml.4 to .5, ..., config.yaml to .1). config.yaml itself is linked, not moved, so that
        there is never a moment without one; the new file then replaces it."""
        config = self._paths.config
        for n in range(KEEP_VERSIONS - 1, 0, -1):
            older = config.with_name(f"{config.name}.{n}")
            if older.exists():
                os.replace(older, config.with_name(f"{config.name}.{n + 1}"))
        if config.exists():
            first = config.with_name(f"{config.name}.1")
            first.unlink(missing_ok=True)
            try:
                os.link(config, first)
            except OSError:   # a file system without hard links
                shutil.copy2(config, first)

    def write_config(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """The whole new config: checked, the older versions moved along, written beside the old one and renamed into place (owner the
        room's, mode 640). A config that fails the check writes nothing."""
        _only(args, "yaml")
        text = _text(args, "yaml", MAX_CONFIG)
        # Held through the write, so that a restart cannot begin half way through one. (The watch takes the write lock to undo, and the
        # watch lock only after it has let the write lock go: the two are never taken the other way round.)
        with self._watch_lock:
            if self._watching:   # the watch may yet put config.yaml.1 back: a change now would be the one undone, or undo the wrong one
                raise JobError(RESTARTING)
            with self._write_lock:
                check_config_yaml(text, self._current_config())
                self._paths.config_dir.mkdir(parents=True, exist_ok=True)
                self._rotate()
                self._place(self._paths.config, text.encode("utf-8"), 0o640)
                self._update_state(last_write=self._host.clock())
        logger.info("Wrote a new config.yaml")
        return {"path": str(self._paths.config)}

    def _current_config(self) -> Optional[Dict[str, Any]]:
        """The config on disk now, as a mapping; None when there is none or it can't be read (nothing to compare a new one with)."""
        try:
            data = yaml.safe_load(self._paths.config.read_text())
        except (OSError, ValueError, yaml.YAMLError):
            return None
        return data if isinstance(data, dict) else None

    def save_credential(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """A Google service-account key or the Zoom credentials, checked to be what they are uploaded as, saved under the config folder
        for the room's user only (mode 600). The answer says what was saved, never the key."""
        _only(args, "kind", "content")
        kind = args.get("kind")
        if not isinstance(kind, str) or kind not in CREDENTIAL_FILES:
            raise JobError("The key must be a Google service-account key or Zoom credentials")
        content = _text(args, "content", MAX_CREDENTIAL)
        try:
            summary = credential_summary(kind, parse_credential(content))
        except CredentialError as e:
            raise JobError(str(e)) from e
        target = self._paths.config_dir / CREDENTIAL_FILES[kind]
        self._paths.config_dir.mkdir(parents=True, exist_ok=True)
        self._place(target, content.encode("utf-8"), 0o600)
        logger.info(f"Saved the {kind} key at {target}")
        return {"path": str(target), "summary": summary}

    # --- the USB stick

    def _usb_listing(self) -> List[Dict[str, Any]]:
        """`*.json` directly in the top folder of each mounted stick (/media/<user>/<label>/), regular files of at most MAX_USB_FILE.
        A link is left out: it could point anywhere on the machine, and the helper reads as root."""
        found = []
        for path in sorted(self._paths.media_root.glob("*/*/*.json")):
            try:
                info = path.lstat()
            except OSError:
                continue
            if path.is_symlink() or not path.is_file() or path.parent.is_symlink() or info.st_size > MAX_USB_FILE:
                continue
            found.append({"path": str(path), "name": path.name, "stick": path.parent.name, "size": info.st_size})
        return found

    def usb_files(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """The key files on the USB sticks plugged in."""
        _only(args)
        return {"files": self._usb_listing()}

    def read_usb_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """One file that usb_files lists, as text. Any other path, however it is spelled, is refused."""
        _only(args, "path")
        path = args.get("path")
        if not isinstance(path, str) or path not in {f["path"] for f in self._usb_listing()}:
            raise JobError("That is not a key file on a USB stick")
        try:
            # Not a link swapped in since the listing, nor a FIFO or a device that would hold the helper in open() or read()
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as e:
            raise JobError("The USB stick could not be read: was it taken out?") from e
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise JobError("That is not a key file on a USB stick")
            with os.fdopen(fd, "rb") as f:
                fd = -1
                raw = f.read(MAX_USB_FILE + 1)
        except OSError as e:
            raise JobError("The USB stick could not be read: was it taken out?") from e
        finally:
            if fd >= 0:
                os.close(fd)
        if len(raw) > MAX_USB_FILE:
            raise JobError("That is not a key file on a USB stick")
        try:
            return {"content": raw.decode("utf-8")}
        except UnicodeDecodeError as e:
            raise JobError("That file is not text: a key file is the .json file the console downloads") from e

    # --- restart and the health watch

    def _read_state(self) -> Dict[str, Any]:
        try:
            state = json.loads(self._paths.state.read_text())
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def _update_state(self, **changes: Any) -> None:
        state = self._read_state()
        state.update(changes)
        self._paths.data_dir.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self._paths.data_dir, prefix=".admin-state.")
        try:
            os.fchmod(fd, 0o644)   # on the open file, not by its name: a name in the folder can be swapped, the descriptor can't
            with os.fdopen(fd, "w") as f:
                fd = -1
                json.dump(state, f)
            os.replace(temp, self._paths.state)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
            raise

    def _record(self, state: str, undone: bool, why: str = "") -> None:
        record = {"state": state, "undone": undone, "at": datetime.fromtimestamp(self._host.clock()).astimezone().isoformat(timespec="seconds")}
        if why:
            record["why"] = why
        self._update_state(restart=record)

    def _control_address(self) -> Tuple[str, int]:
        """Where the room page listens in the config on disk now (the one the room restarted with): its host, or the loopback when it
        listens on every address, and its port, 8080 when it says none."""
        try:
            control = (yaml.safe_load(self._paths.config.read_text()) or {}).get("control") or {}
            host, port = control.get("host", ""), control.get("port", DEFAULT_CONTROL_PORT)
        except (OSError, yaml.YAMLError, AttributeError):
            return LOOPBACK, DEFAULT_CONTROL_PORT
        host = host.strip() if isinstance(host, str) else ""
        port = port if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536 else DEFAULT_CONTROL_PORT
        return (LOOPBACK if host in ANY_ADDRESS else host), port

    def _comes_back(self) -> bool:
        """Ask GET /api/status every HEALTH_INTERVAL_S until it answers, for HEALTH_TIMEOUT_S on the monotonic clock."""
        host, port = self._control_address()
        started = self._host.monotonic()
        while True:
            self._host.sleep(HEALTH_INTERVAL_S)
            if self._host.probe(host, port):
                return True
            if self._host.monotonic() - started >= HEALTH_TIMEOUT_S:
                return False

    def _restart_room(self) -> None:
        code, output = self._host.run(["systemctl", "restart", "croom"])
        if code != 0:
            logger.warning(f"systemctl restart croom: exit {code}: {output.strip()}")

    def _undo_last_write(self) -> None:
        """Put config.yaml.1 back as config.yaml, written as any config is. The change is forgotten, so it is undone once only."""
        with self._write_lock:
            previous = self._paths.config.with_name(f"{self._paths.config.name}.1").read_bytes()
            self._place(self._paths.config, previous, 0o640)
            self._update_state(last_write=None)

    def _watch(self, asked_at: float, restart_first: bool = False) -> None:
        """After a restart: the room answers within 2 minutes, or, when its config was changed within the 10 minutes before the restart
        was asked for and the one before it is kept, that one is put back and the room restarted again. With `restart_first`, the
        restart itself is done here first, in the background: systemctl waits for the room software to stop, and the room software
        waits for the request that asked for the restart, so the answer must not wait for systemctl. Never raises: it is the last thing
        between a bad change and a room that stays down."""
        try:
            if restart_first:
                self._restart_room()
                self._record("watching", False)
            if self._comes_back():
                self._record("healthy", False)
                return
            last_write = self._read_state().get("last_write")
            previous = self._paths.config.with_name(f"{self._paths.config.name}.1")
            recent = isinstance(last_write, (int, float)) and asked_at - last_write <= UNDO_WINDOW_S
            if not (recent and previous.exists()):
                logger.error("The room did not answer within 2 minutes of a restart, and there is no recent change to undo")
                self._record("down", False, "The room did not answer within 2 minutes of a restart")
                return
            logger.error("The room did not answer within 2 minutes of a restart: putting back config.yaml.1")
            self._undo_last_write()
            self._restart_room()
            why = "The room did not answer within 2 minutes of a restart with the new settings: the settings before them are back"
            self._record("undone", True, why)
            if not self._comes_back():
                logger.error("The room has not answered with the settings before the change either")
                self._record("undone", True, why + ", but it has not answered since either")
        except Exception:
            logger.exception("The health watch failed")
            self._record("down", False, "The helper could not watch the room come back")
        finally:
            with self._watch_lock:
                self._watching = False

    def restart(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Restart the room software and watch it come back, both in the background: the answer goes at once. (systemctl restart waits
        for the room software to stop, and the room software, stopping, waits for the request that asked: answering after systemctl
        would hold both up until the room software's own stop gave up on that request.)"""
        _only(args)
        with self._watch_lock:
            if self._watching:
                raise JobError("The room is restarting: wait for it to come back")
            self._watching = True
        try:
            asked_at = self._host.clock()
            self._update_state(watch_asked_at=asked_at)
            self._record("restarting", False)
            self._host.background(lambda: self._watch(asked_at, restart_first=True))
        except BaseException:
            with self._watch_lock:
                self._watching = False
            raise
        return {"restarting": True}

    def resume(self) -> None:
        """At the helper's start: a watch that the helper's own stop cut short (the record still says restarting or watching) goes on,
        from when its restart was asked for, so that a bad change is still undone. The room is given its full 2 minutes again."""
        state = self._read_state()
        record = state.get("restart")
        asked_at = state.get("watch_asked_at")
        if not (isinstance(record, dict) and record.get("state") in ("restarting", "watching") and isinstance(asked_at, (int, float))):
            return
        with self._watch_lock:
            if self._watching:
                return
            self._watching = True
        logger.warning("A restart was being watched when the helper stopped: watching again")
        self._host.background(lambda: self._watch(asked_at))

    def restart_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """How the last restart went: restarting (asked for, systemctl not yet done), watching, healthy, undone (with why) or down (with
        why); state none before any."""
        _only(args)
        record = self._read_state().get("restart")
        return record if isinstance(record, dict) else {"state": "none", "undone": False}

    def reboot(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Reboot the TV Pi a moment after answering: the answer would otherwise wait on a machine that is going down."""
        _only(args)
        self._host.background(self._reboot_soon)
        return {"rebooting": True}

    def _reboot_soon(self) -> None:
        try:
            self._host.sleep(REBOOT_DELAY_S)
            code, output = self._host.run(["systemctl", "reboot"])
            if code != 0:
                logger.error(f"The TV Pi did not reboot: {output.strip() or f'systemctl said {code}'}")
        except Exception:
            logger.exception("The reboot failed")

    def version(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """The commit this TV Pi runs, as the installer or an update wrote it; unknown when nothing did."""
        _only(args)
        try:
            version = self._paths.version_file.read_text().strip()
        except OSError:
            version = ""
        return {"version": version or "unknown"}
