"""
The devices that check in with the TV Pi (spec 2026-10-29, section 7): the touch Pi's agent and the door sign. Each keeps an id and a
token of its own, both random, and calls POST /api/devices/checkin every 5 seconds with its id, kind, version, network and the state of
its last job; the answer is its next job, if any.

A device the TV Pi does not know becomes a pending entry, which Settings › Software lists under "Devices waiting to join". Allow (behind
the PIN) lets it in; from then on every check-in must carry the token it joined with, which the TV Pi keeps only as a SHA-256 hash (the
token is 256 random bits, so a plain hash is enough). Refuse removes it and remembers its id, so that it does not come back to the list
by itself 5 seconds later; Forget removes an entry and remembers nothing (a device replaced by another). A pending device gets no jobs.
At most MAX_PENDING devices wait at once: a pending entry not seen for PENDING_FORGET_S makes room, and beyond that an unknown device is
refused, so that the LAN cannot fill the registry.

A device does one job at a time: queue() refuses another while one is waiting, handed out or running. Jobs live in memory only (a Wi-Fi
password is one of their arguments): a TV Pi that restarts forgets a job it had not handed out yet, and takes the device's word for the
result of one it had. A job handed out but not mentioned in the device's next check-ins is handed out again after RESEND_S (the answer
was lost); a running job the device stops mentioning has ended without a result.

The registry (devices, their hashed tokens, last seen, last result, and the refused ids) is persisted in the data directory, mode 600;
last seen is written at most every SAVE_EVERY_S, the rest of an allowed device's at once. What a pending device says is written only with
the next of those throttled writes, never at once: anyone on the LAN can make up pending devices, and must not wear the card out. A
devices.json that can't be read as one is moved aside (devices.json.bad-<time>) rather than written over, so that a person can look at it.

A wifi_scan's result (the networks the device found) is kept in memory with the device, for Settings › Network. The protocol is the same for every kind of device: the jobs' arguments
are checked here (check_job) and again by the device.
"""

import contextlib
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

KINDS = {"touch": "touch Pi", "sign": "door sign"}            # a device's kind, and how the page names it
COMMIT_KINDS = ("touch",)          # the kinds that run a commit of main (updated to one); the door sign runs an APK (Task 11)
DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
COMMIT = re.compile(r"[0-9a-f]{40}")
JOB_ID = re.compile(r"[0-9a-f]{16}")
TOKEN_HEADER = "X-Croom-Device-Token"
INTERVAL_S = 5                    # how often a device checks in (it is told so in each answer)
ONLINE_S = 20                     # a device that has checked in within this is "online"; otherwise "Not seen since <time>"
RESEND_S = 15                     # a job handed out that the device has not mentioned since is handed out again
MAX_PENDING = 10
PENDING_FORGET_S = 10 * 60
MAX_REFUSED = 50
SAVE_EVERY_S = 60
MAX_WORDS = 300
MAX_TEXT = 64
MAX_NETWORK = 4096
MAX_NETWORKS = 30                 # a wifi_scan's result: the networks kept
DONE, FAILED, RUNNING = "done", "failed", "running"

WIFI_PASSWORD = re.compile(r"[\x20-\x7e]{8,63}|[0-9A-Fa-f]{64}")


class CheckinError(Exception):
    """A check-in or a Settings request refused: `status` is the HTTP status, the message the words for the person."""

    def __init__(self, words: str, status: int):
        super().__init__(words)
        self.status = status


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def label(kind: str) -> str:
    return KINDS.get(kind, "device")


def _ssid(args: Dict[str, Any]) -> str:
    ssid = args.get("ssid")
    if not isinstance(ssid, str) or not ssid.strip() or len(ssid.encode("utf-8")) > 32 or any(ord(c) < 32 for c in ssid):
        raise CheckinError("Choose a network (a name of 1 to 32 characters)", 400)
    return ssid


def _no_args(args: Dict[str, Any]) -> Dict[str, Any]:
    return {}


def _update_args(args: Dict[str, Any]) -> Dict[str, Any]:
    commit = args.get("commit")
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise CheckinError("Send the version to install (a commit)", 400)
    return {"commit": commit}


def _connect_args(args: Dict[str, Any]) -> Dict[str, Any]:
    ssid = _ssid(args)
    password = args.get("password", "")
    if password is None:
        password = ""
    if not isinstance(password, str) or (password and not WIFI_PASSWORD.fullmatch(password)):
        raise CheckinError("A Wi-Fi password is 8 to 63 letters, digits and symbols, or a key of 64 characters 0 to 9 and A to F", 400)
    hidden = args.get("hidden", False)
    if not isinstance(hidden, bool):
        raise CheckinError("Hidden must be true or false", 400)
    return {"ssid": ssid, "password": password, "hidden": hidden}


def _forget_args(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"ssid": _ssid(args)}


JOBS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "reboot": _no_args,
    "update": _update_args,
    "wifi_scan": _no_args,
    "wifi_connect": _connect_args,
    "wifi_forget": _forget_args,
}
SECRET_ARGS = ("password",)       # never shown back on a page


def check_job(job: Any, args: Any) -> Dict[str, Any]:
    """The arguments of `job` as the device is sent them, or CheckinError (400) naming what is wrong."""
    if job not in JOBS:
        raise CheckinError("There is no such job", 400)
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise CheckinError("Send the job's arguments as an object", 400)
    return JOBS[job](args)


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c.isprintable())[:limit]


def scan_result(result: Any) -> Optional[List[Dict[str, Any]]]:
    """A wifi_scan's result as a device reported it ({"networks": [...]}), cleaned: at most MAX_NETWORKS networks, each {"ssid",
    "signal", "security", "open", "saved", "in_use"}; None when it is not one."""
    networks = result.get("networks") if isinstance(result, dict) else None
    if not isinstance(networks, list):
        return None
    clean = []
    for network in networks[:MAX_NETWORKS]:
        if not isinstance(network, dict) or not isinstance(network.get("ssid"), str):
            continue
        ssid = network["ssid"]
        if not ssid or len(ssid.encode("utf-8", "replace")) > 32:
            continue
        signal = network.get("signal")
        clean.append({"ssid": ssid, "signal": signal if isinstance(signal, int) and not isinstance(signal, bool) and 0 <= signal <= 100 else 0,
                      "security": _text(network.get("security"), 40), "open": network.get("open") is True,
                      "saved": network.get("saved") is True, "in_use": network.get("in_use") is True})
    return clean


def _iso(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


class Registry:
    """
    Args:
        path: the registry's file (devices.json in the data directory); None keeps it in memory only.
        clock: wall-clock seconds (last seen, which the page shows as a time).
        new_id: a new job's id.
    """

    def __init__(self, path: Optional[Path] = None, clock: Callable[[], float] = time.time,
                 new_id: Callable[[], str] = lambda: secrets.token_hex(8)):
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._new_id = new_id
        self._lock = threading.Lock()
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._refused: List[str] = []
        self._jobs: Dict[str, Dict[str, Any]] = {}       # device id -> its job (waiting, handed out or running): in memory only
        self._scans: Dict[str, Dict[str, Any]] = {}      # device id -> its last wifi_scan's networks and when: in memory only
        self._saved_at = 0.0
        self._dirty = False
        self._load()

    # --- the file

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except OSError as e:
            logger.error(f"Could not read the devices that check in from {self.path}: {e}")
            return
        except ValueError as e:
            self._set_aside(f"it is not JSON ({e})")
            return
        if not isinstance(data, dict):
            self._set_aside("it is not a JSON object")
            return
        devices = data.get("devices") if isinstance(data, dict) else None
        for device_id, entry in (devices or {}).items():
            if (DEVICE_ID.fullmatch(str(device_id)) and isinstance(entry, dict) and entry.get("kind") in KINDS
                    and entry.get("state") in ("pending", "allowed") and isinstance(entry.get("token_hash"), str)):
                self._devices[device_id] = entry
        refused = data.get("refused") if isinstance(data, dict) else None
        self._refused = [r for r in (refused or []) if isinstance(r, str) and DEVICE_ID.fullmatch(r)][-MAX_REFUSED:]

    def _set_aside(self, why: str) -> None:
        """Move a devices.json that can't be read out of the way (devices.json.bad-<time>), so that the next write does not lose it."""
        stamp = datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        aside = self.path.with_name(f"{self.path.name}.bad-{stamp}")
        try:
            os.replace(self.path, aside)
        except OSError as e:
            logger.error(f"Could not read {self.path} ({why}), nor move it aside: {e}")
            self.path = None   # kept in memory only: the file is not written over
            return
        logger.error(f"Could not read {self.path} ({why}): moved it aside to {aside}; the devices must be allowed again")

    def _save(self, force: bool = True) -> None:
        """Write the registry (at once when `force`; otherwise only when SAVE_EVERY_S has gone by since the last write)."""
        self._dirty = True
        if self.path is None:
            return
        now = self._clock()
        if not force and now - self._saved_at < SAVE_EVERY_S:
            return
        data = {"devices": self._devices, "refused": self._refused}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=".devices-", dir=str(self.path.parent))
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=1, sort_keys=True)
                os.replace(temp, self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp)
                raise
        except OSError as e:
            logger.error(f"Could not save the devices that check in to {self.path}: {e}")
            return
        self._saved_at = now
        self._dirty = False

    def flush(self) -> None:
        """Write what has not been written yet (last seen), as the agent stops."""
        with self._lock:
            if self._dirty:
                self._save()

    # --- check-in

    def checkin(self, body: Any, token: str, address: str = "") -> Dict[str, Any]:
        """
        One check-in: `body` is the device's JSON ({"id", "kind", "version", "name", "network", "job"}), `token` its header, `address`
        where it called from. Answers {"state": "pending"} or {"state": "allowed", "job": the next job or None, "interval"}; raises
        CheckinError (400 a body that is not one, 401 no token, 403 a refused device or a token that is not the one it joined with, 429
        too many waiting).
        """
        if not isinstance(body, dict):
            raise CheckinError("Send a JSON object", 400)
        device_id, kind = body.get("id"), body.get("kind")
        if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
            raise CheckinError("Send the device's id", 400)
        if kind not in KINDS:
            raise CheckinError("Send the device's kind (touch or sign)", 400)
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise CheckinError("Send the device's token", 401)
        hashed = hash_token(token)
        with self._lock:
            now = self._clock()
            if device_id in self._refused:
                raise CheckinError("This device was refused", 403)
            entry = self._devices.get(device_id)
            if entry is None:
                self._make_room(now)
                if sum(1 for e in self._devices.values() if e["state"] == "pending") >= MAX_PENDING:
                    raise CheckinError("Too many devices are waiting to join", 429)
                entry = {"kind": kind, "state": "pending", "token_hash": hashed, "first_seen": now, "last_seen": now}
                self._devices[device_id] = entry
                self._seen(entry, body, address, now)
                self._save(force=False)   # a device waiting to join is written with the next throttled write, never at once
                logger.info(f"A {label(kind)} asks to join: {entry.get('name') or device_id} at {address}")
                return {"state": "pending", "interval": INTERVAL_S}
            if not hmac.compare_digest(entry["token_hash"], hashed):
                raise CheckinError("This device's token is not the one it joined with", 403)
            changed = self._seen(entry, body, address, now)
            if entry["state"] == "pending":
                self._save(force=False)
                return {"state": "pending", "interval": INTERVAL_S}
            changed = self._report(device_id, entry, body.get("job"), now) or changed
            job = self._hand_out(device_id, now)
            self._save(force=changed)
            return {"state": "allowed", "job": job, "interval": INTERVAL_S}

    def _make_room(self, now: float) -> None:
        for device_id, entry in list(self._devices.items()):
            if entry["state"] == "pending" and now - entry.get("last_seen", 0) > PENDING_FORGET_S:
                del self._devices[device_id]

    @staticmethod
    def _seen(entry: Dict[str, Any], body: Dict[str, Any], address: str, now: float) -> bool:
        """Note what the device says of itself; True when something other than the time changed (written at once)."""
        network = body.get("network")
        if not isinstance(network, dict) or len(json.dumps(network)) > MAX_NETWORK:
            network = {}
        fresh = {"version": _text(body.get("version")), "name": _text(body.get("name")), "address": _text(address),
                 "network": network}
        changed = any(entry.get(key) != value for key, value in fresh.items() if key != "network")
        entry.update(fresh)
        entry["last_seen"] = now
        return changed

    def _report(self, device_id: str, entry: Dict[str, Any], report: Any, now: float) -> bool:
        """The device's word on its last job. True when the registry's record changed."""
        if not isinstance(report, dict) or not isinstance(report.get("id"), str):
            report = None
        job = self._jobs.get(device_id)
        state = report.get("state") if report else None
        words = _text(report.get("words"), MAX_WORDS) if report else ""
        if job is not None and report is not None and report["id"] == job["id"]:
            if state in (DONE, FAILED):
                if job["type"] == "wifi_scan" and state == DONE:
                    networks = scan_result(report.get("result"))
                    if networks is not None:
                        self._scans[device_id] = {"networks": networks, "at": _iso(now)}
                self._close(device_id, entry, job, state, words, now)
                return True
            job["state"] = RUNNING
            job["words"] = words
            return False
        if job is not None and job["state"] == RUNNING:
            # The device took it, and now speaks of something else: it ended without a result
            self._close(device_id, entry, job, FAILED, f"The {label(entry['kind'])} stopped before it finished", now)
            return True
        last = entry.get("last") or {}
        if report is not None and state in (DONE, FAILED) and JOB_ID.fullmatch(report["id"]) and last.get("id") != report["id"]:
            # A result for a job this TV Pi no longer holds (handed out before it restarted)
            entry["last"] = {"id": report["id"], "type": _text(report.get("type")), "args": {}, "state": state, "words": words, "at": now}
            return True
        return False

    def _close(self, device_id: str, entry: Dict[str, Any], job: Dict[str, Any], state: str, words: str, now: float) -> None:
        """The job has ended: it becomes the device's last result, and the device may be given another."""
        entry["last"] = {"id": job["id"], "type": job["type"], "args": _public_args(job["args"]), "state": state, "words": words,
                         "at": now}
        self._jobs.pop(device_id, None)

    def _hand_out(self, device_id: str, now: float) -> Optional[Dict[str, Any]]:
        job = self._jobs.get(device_id)
        if job is None:
            return None
        if job["state"] == "queued" or (job["state"] == "sent" and now - job["sent_at"] >= RESEND_S):
            job["state"] = "sent"
            job["sent_at"] = now
            return {"id": job["id"], "type": job["type"], "args": copy.deepcopy(job["args"])}
        return None

    # --- Settings (behind the PIN)

    def _entry(self, device_id: str) -> Dict[str, Any]:
        entry = self._devices.get(device_id) if isinstance(device_id, str) else None
        if entry is None:
            raise CheckinError(f"There is no device {device_id}", 404)
        return entry

    def allow(self, device_id: str) -> Dict[str, Any]:
        with self._lock:
            entry = self._entry(device_id)
            if entry["state"] != "allowed":
                entry["state"] = "allowed"
                entry["allowed_at"] = self._clock()
                self._save()
                logger.info(f"The {label(entry['kind'])} {entry.get('name') or device_id} may join")
            return self._public(device_id, entry, self._clock())

    def refuse(self, device_id: str) -> None:
        """Remove the device and remember its id: its check-ins are refused from now on."""
        with self._lock:
            entry = self._entry(device_id)
            del self._devices[device_id]
            self._jobs.pop(device_id, None)
            self._scans.pop(device_id, None)
            self._refused = (self._refused + [device_id])[-MAX_REFUSED:]
            self._save()
            logger.info(f"The {label(entry['kind'])} {entry.get('name') or device_id} was refused")

    def forget(self, device_id: str) -> None:
        """Remove the device and remember nothing: if it is still there, it asks to join again."""
        with self._lock:
            entry = self._entry(device_id)
            del self._devices[device_id]
            self._jobs.pop(device_id, None)
            self._scans.pop(device_id, None)
            self._save()
            logger.info(f"The {label(entry['kind'])} {entry.get('name') or device_id} was forgotten")

    def queue(self, device_id: str, job: str, args: Any = None) -> Dict[str, Any]:
        """Give the device a job ({"type", "args"} checked by check_job). Refused for a device that has not joined, and while it has
        another one."""
        clean = check_job(job, args)
        with self._lock:
            entry = self._entry(device_id)
            if entry["state"] != "allowed":
                raise CheckinError(f"This {label(entry['kind'])} has not joined yet: allow it first", 409)
            if device_id in self._jobs:
                raise CheckinError(f"The {label(entry['kind'])} is busy: wait for it to finish", 409)
            now = self._clock()
            self._jobs[device_id] = {"id": self._new_id(), "type": job, "args": clean, "state": "queued", "words": "", "at": now,
                                     "sent_at": 0.0}
            return self._public_job(self._jobs[device_id])

    def busy(self, device_id: str) -> bool:
        with self._lock:
            return device_id in self._jobs

    def get(self, device_id: str) -> Dict[str, Any]:
        with self._lock:
            return self._public(device_id, self._entry(device_id), self._clock())

    def devices(self) -> List[Dict[str, Any]]:
        """The devices allowed in, oldest first, as the page shows them."""
        with self._lock:
            now = self._clock()
            return [self._public(i, e, now) for i, e in sorted(self._devices.items(), key=lambda p: p[1].get("first_seen", 0))
                    if e["state"] == "allowed"]

    def waiting(self) -> List[Dict[str, Any]]:
        """The devices waiting to join, oldest first (those not seen for PENDING_FORGET_S are gone)."""
        with self._lock:
            now = self._clock()
            return [self._public(i, e, now) for i, e in sorted(self._devices.items(), key=lambda p: p[1].get("first_seen", 0))
                    if e["state"] == "pending" and now - e.get("last_seen", 0) <= PENDING_FORGET_S]

    def seen_since(self, kind: str, since: float) -> bool:
        """Whether an allowed device of `kind` has checked in at or after `since` (wall-clock seconds)."""
        with self._lock:
            return any(e["state"] == "allowed" and e["kind"] == kind and e.get("last_seen", 0) >= since for e in self._devices.values())

    def known(self, kind: str) -> bool:
        """Whether a device of `kind` has joined (been allowed)."""
        with self._lock:
            return any(e["state"] == "allowed" and e["kind"] == kind for e in self._devices.values())

    @staticmethod
    def _public_job(job: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": job["id"], "type": job["type"], "args": _public_args(job["args"]), "state": job["state"],
                "words": job.get("words", ""), "at": _iso(job.get("at"))}

    def _public(self, device_id: str, entry: Dict[str, Any], now: float) -> Dict[str, Any]:
        last_seen = entry.get("last_seen")
        job = self._jobs.get(device_id)
        last = entry.get("last")
        return {
            "id": device_id, "kind": entry["kind"], "label": label(entry["kind"]), "state": entry["state"],
            "name": entry.get("name", ""), "address": entry.get("address", ""), "version": entry.get("version", ""),
            "network": copy.deepcopy(entry.get("network") or {}),
            "first_seen": _iso(entry.get("first_seen")), "last_seen": _iso(last_seen),
            "seen_ago": None if last_seen is None else max(0, int(now - last_seen)),
            "online": last_seen is not None and now - last_seen <= ONLINE_S,
            "job": self._public_job(job) if job else None,
            "last": dict(last, at=_iso(last.get("at"))) if isinstance(last, dict) else None,
            "scan": copy.deepcopy(self._scans.get(device_id)),
        }


def _public_args(args: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in (args or {}).items() if key not in SECRET_ARGS}
