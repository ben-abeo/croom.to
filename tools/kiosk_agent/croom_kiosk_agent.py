#!/usr/bin/env python3
"""
The touch Pi's agent (room settings spec 2026-10-29, sections 6 and 7): it checks in with the room's TV Pi every 5 seconds and does the
jobs Settings gives it there. Standard-library Python 3.11 only: it runs with the system's python3, as root, under systemd
(croom-kiosk-agent.service, which installer/install-kiosk.sh writes). Nothing listens on the touch Pi: it only calls the TV Pi.

Check-in: POST <room page>/api/devices/checkin with {"id", "kind": "touch", "version", "name", "network", "job"} and the header
X-Croom-Device-Token. The id and the token are random, made at the first start and kept in a root-only file; the TV Pi lists a device it
does not know under "Devices waiting to join" until someone with the PIN taps Allow. The answer may carry a job:

- reboot: systemctl reboot (the result, "Rebooted", is told at the first check-in after it).
- update {commit}: a commit of main. The agent checks it is one itself (it fetches main from GitHub into its own clone and asks git
  whether the commit is in main's history) rather than taking the TV Pi's word; it puts that commit's files in a release folder of
  their own beside the running one, points `current` at it, runs that release's kiosk setup (install-kiosk.sh, as the installer does),
  and reboots. The release that was running stays the `good` one, and systemd runs the agent from `good`: after the reboot it is the
  old, known-good agent that starts the new one (--trial) and waits up to 5 minutes for it to check in with the TV Pi. When it has, the
  new release becomes `good` and the agent starts over from it; when it has not, `current` goes back to the old release, the old kiosk
  setup is run again, and the touch Pi reboots onto it.
- wifi_scan: the networks in range (the job's result, which the TV Pi keeps for Settings › Network).
- wifi_connect {ssid, password, hidden}: join a network with the safety net of spec section 7: the connection the touch Pi was on is
  kept, the new network is joined, and within 90 seconds the touch Pi must reach the room (GET <room page>/api/status); otherwise it
  goes back to the network before and says why. The password goes into NetworkManager's own file for the network (root's, mode 600),
  never on a command line. The change's record (wifi.json) is written before each step, so that a power cut leaves the touch Pi on the
  network before unless the new one was proven (the agent's next start finishes or undoes it).
- wifi_forget {ssid}: forget a saved network (not the one the touch Pi is on: it would be cut off).

The Wi-Fi work is the TV Pi's helper's own (croom/admin/nmcli.py, standard library only), loaded from this release by its path. Each
check-in tells the TV Pi how the touch Pi is connected (Wi-Fi or Ethernet, the network, its signal, the networks it knows, the last
change), read from NetworkManager at most every STATUS_EVERY_S.

Every step of an update is written to update.json before it is taken, with the file written whole (a temporary file, fsync, rename), so
that a power cut at any moment leaves a state the next start finishes or undoes: cut off while building, the half-built folder is
removed and nothing else was touched; while switching, the touch Pi goes back to the old release; during the 5 minutes, the old agent
counts them again from the next start (at most MAX_TRIALS starts); while going back, it goes back again. No shell is run: every command
is an argument list.

Layout (root's): /opt/croom-kiosk/{repo.git, releases/<commit>/, current -> releases/<commit>, good -> releases/<commit>};
/var/lib/croom-kiosk/ (mode 700): config.json {url, user} (the installer's), identity.json {id, token} (mode 600), update.json, job.json
(the last job and its state, told to the TV Pi at each check-in), trial-ok (the new agent's word that it checked in), wifi.json (the
last Wi-Fi change, mode 600).
"""

import argparse
import importlib.util
import json
import logging
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("croom-kiosk-agent")


def _load_nmcli():
    """croom/admin/nmcli.py of this release (or clone), by its path: the TV Pi's helper's Wi-Fi work, standard library only."""
    path = Path(__file__).resolve().parents[2] / "src" / "croom" / "admin" / "nmcli.py"
    spec = importlib.util.spec_from_file_location("croom_kiosk_nmcli", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


nmcli = _load_nmcli()

BASE = "/opt/croom-kiosk"
STATE = "/var/lib/croom-kiosk"
KIND = "touch"
TOKEN_HEADER = "X-Croom-Device-Token"
INTERVAL_S = 5
TRIAL_S = 5 * 60                  # the new version has this long after the reboot to check in
MAX_TRIALS = 3                    # starts of the agent in one update's trial (a power cut, a crash) before it goes back for good
CHILD_RESTART_S = 5
COMMAND_TIMEOUT_S = 60
LONG_TIMEOUT_S = 15 * 60          # git fetch from GitHub, the kiosk setup (which may install a package)
COMMIT = re.compile(r"[0-9a-f]{40}")
JOB_ID = re.compile(r"[0-9a-f]{16}")
MAIN = "refs/remotes/origin/main"
STATUS_EVERY_S = 30               # how often the check-in's Wi-Fi status is read from NetworkManager
RESULT_FOR_S = 60                 # how long a scan's networks go with the check-ins after it (the TV Pi keeps them from the first)
NM_CONNECTIONS = "/etc/NetworkManager/system-connections"
AGENT_FILE = Path("tools") / "kiosk_agent" / "croom_kiosk_agent.py"
SETUP_FILE = Path("installer") / "install-kiosk.sh"
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

Run = Callable[..., Tuple[int, str]]


def short(name: Optional[str]) -> str:
    return name[:7] if isinstance(name, str) and COMMIT.fullmatch(name) else str(name or "unknown")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


# --- What the agent does to the machine, each replaceable in the tests

def run_command(args: List[str], env: Optional[Dict[str, str]] = None, timeout: float = COMMAND_TIMEOUT_S) -> Tuple[int, str]:
    """Run a command (an argument list, never a shell): its exit status and what it printed (stdout and stderr)."""
    try:
        done = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]} took longer than {int(timeout)} seconds"
    except OSError as e:
        return 127, f"{args[0]}: {e}"
    return done.returncode, done.stdout or ""


def http_post(url: str, body: bytes, headers: Dict[str, str], timeout: float = 10) -> Tuple[int, bytes]:
    """POST JSON: (status, body). An HTTP error status is an answer too; no answer at all raises OSError."""
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers=dict(headers, **{"Content-Type": "application/json", "User-Agent": "croom-kiosk-agent"}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(65536)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(65536)
        except OSError:
            return e.code, b""


def room_answers(url: str) -> bool:
    """Whether the room page answers GET /api/status: the touch Pi can do its job on the network it is on."""
    request = urllib.request.Request(urllib.parse.urljoin(url, "/api/status"), headers={"User-Agent": "croom-kiosk-agent"})
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
        return False


def network_status(url: str) -> Dict[str, Any]:
    """The address this touch Pi reaches the TV Pi from (no packet is sent: a UDP socket only picks its route)."""
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((socket.gethostbyname(host), 9))
            return {"address": s.getsockname()[0]}
    except OSError:
        return {}


class Host:
    """Everything the agent does outside its own folders: commands, the reboot, HTTP, time, processes. The tests give fakes."""

    def __init__(self, run: Run = run_command, post=http_post, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 spawn=None, execv=os.execv, sync: Callable[[], None] = os.sync, python: str = sys.executable,
                 network: Callable[[str], Dict[str, Any]] = network_status, hostname: Callable[[], str] = socket.gethostname,
                 euid: Callable[[], int] = os.geteuid, room_status: Callable[[str], bool] = room_answers,
                 nm_connections: str = NM_CONNECTIONS):
        self.run = run
        self.post = post
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.spawn = spawn or (lambda args: subprocess.Popen(args, stdin=subprocess.DEVNULL))
        self.execv = execv
        self.sync = sync
        self.python = python
        self.network = network
        self.hostname = hostname
        self.euid = euid
        self.room_status = room_status
        self.nm_connections = nm_connections


class Paths:
    def __init__(self, base: str = BASE, state: str = STATE):
        self.base = Path(base)
        self.repo = self.base / "repo.git"
        self.releases = self.base / "releases"
        self.current = self.base / "current"
        self.good = self.base / "good"
        self.state = Path(state)
        self.config = self.state / "config.json"
        self.identity = self.state / "identity.json"
        self.update = self.state / "update.json"
        self.job = self.state / "job.json"
        self.trial_ok = self.state / "trial-ok"
        self.wifi = self.state / "wifi.json"

    def release(self, name: str) -> Path:
        """A release's folder; a name that is not a commit is never made into a path."""
        if not isinstance(name, str) or not COMMIT.fullmatch(name):
            raise ValueError(f"not a release name: {name!r}")
        return self.releases / name

    def agent(self, release: str) -> Path:
        return self.release(release) / AGENT_FILE


class JsonStore:
    """The Wi-Fi change's record in a file of its own, written whole, root's alone."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Dict[str, Any]:
        return read_json(self.path)

    def save(self, record: Dict[str, Any]) -> None:
        write_json(self.path, record)


class Failed(Exception):
    """A job that did not happen: the words say why, for the person reading the Software page."""


# --- Files written whole

def write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    """Write `data` to `path` whole: a temporary file beside it, flushed to the card, renamed over it, and the folder flushed."""
    temp = path.with_name(f".{path.name}.tmp")
    with _suppress_missing():
        os.unlink(temp)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, json.dumps(data, indent=1, sort_keys=True).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)
    sync_dir(path.parent)


def read_json(path: Path) -> Dict[str, Any]:
    """A JSON object from a file (not through a link); {} when it is missing or not one."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "rb") as f:
            data = json.loads(f.read(1024 * 1024).decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def sync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def point(link: Path, release: str) -> None:
    """Point `link` at releases/<release> in one step (a new link renamed over the old one)."""
    temp = link.with_name(f".{link.name}-{os.getpid()}")
    with _suppress_missing():
        os.unlink(temp)
    os.symlink(f"releases/{release}", temp)
    os.replace(temp, link)
    sync_dir(link.parent)


def link_target(link: Path) -> Optional[str]:
    """The release a link points at, or None."""
    try:
        name = os.path.basename(os.readlink(link).rstrip("/"))
    except OSError:
        return None
    return name if COMMIT.fullmatch(name) else None


class _suppress_missing:
    def __enter__(self):
        return self

    def __exit__(self, kind, value, trace):
        return kind is not None and issubclass(kind, FileNotFoundError)


# --- The agent

class Agent:
    """
    Args:
        paths: its folders.
        host: the machine (commands, HTTP, time, processes).
        version: the release this copy of the agent runs from (a commit), or "unknown".
        trial: started by the good agent to prove a new version: it checks in, says so in trial-ok, and does no job.
    """

    def __init__(self, paths: Paths, host: Host, version: str, trial: bool = False):
        self.paths = paths
        self.host = host
        self.version = version
        self.trial = trial
        self.config = read_json(paths.config)
        self.url = str(self.config.get("url") or "")
        self.user = str(self.config.get("user") or "")
        self.identity: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._job: Dict[str, Any] = read_json(paths.job)       # the last job: what each check-in tells the TV Pi
        self._said: Optional[str] = None                        # the last trouble logged, so that it is logged once
        self.stopping = False
        self.wifi = nmcli.Wifi(run=lambda args: host.run(args), store=JsonStore(paths.wifi), connections=Path(host.nm_connections),
                               name="touch Pi", clock=host.clock, monotonic=host.monotonic, sleep=host.sleep)
        self._wifi_status: Optional[Dict[str, Any]] = None      # what the check-ins say of the Wi-Fi, read every STATUS_EVERY_S
        self._wifi_status_at = 0.0
        self._result_until = 0.0                                # until when the last job's result goes with the check-ins

    # --- start

    def start(self) -> Optional[str]:
        """Before the check-ins: the folders, the identity, and (for the good agent) what an update left to do. Answers "trial" when a
        new version is to be proven now (supervise()), "rebooting" when the touch Pi is going down, else None."""
        self.paths.state.mkdir(parents=True, exist_ok=True)
        os.chmod(self.paths.state, 0o700)
        self.identity = self._identity()
        if self.trial:
            return None
        self._recover_wifi()
        plan = self.recover()
        if plan is None:
            self._recover_job()
            self.sweep()
        return plan

    def _identity(self) -> Dict[str, str]:
        known = read_json(self.paths.identity)
        if (isinstance(known.get("id"), str) and re.fullmatch(r"[0-9a-f]{32}", known["id"]) and isinstance(known.get("token"), str)
                and re.fullmatch(r"[A-Za-z0-9_-]{43}", known["token"])):
            return {"id": known["id"], "token": known["token"]}
        made = {"id": secrets.token_hex(16), "token": secrets.token_urlsafe(32)}
        write_json(self.paths.identity, made)
        logger.info(f"This touch Pi is {made['id']}: it waits to join until Allow is tapped in Settings › Software")
        return made

    # --- check-in

    def report(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._job
            if not JOB_ID.fullmatch(str(job.get("id", ""))):
                return None
            report = {key: job.get(key) for key in ("id", "type", "state", "words")}
            if job.get("result") is not None and self.host.monotonic() < self._result_until:
                report["result"] = job["result"]
            return report

    def _set_job(self, **fields: Any) -> None:
        with self._lock:
            self._job = dict(self._job, **fields, at=now_iso())
            job = dict(self._job)
        try:
            write_json(self.paths.job, job)
        except OSError as e:
            logger.error(f"Could not keep the job's state in {self.paths.job}: {e}")

    def checkin(self) -> Optional[Dict[str, Any]]:
        """One check-in: the TV Pi's answer, or None when there was none to use."""
        body = {"id": self.identity["id"], "kind": KIND, "version": self.version, "name": self.host.hostname()[:64],
                "network": self.network(), "job": self.report()}
        target = urllib.parse.urljoin(self.url, "/api/devices/checkin")
        try:
            status, raw = self.host.post(target, json.dumps(body).encode("utf-8"), {TOKEN_HEADER: self.identity["token"]})
        except OSError as e:
            self._trouble(f"Can't reach the room at {self.url}: {e}")
            return None
        try:
            answer = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            answer = {}
        if status != 200 or not isinstance(answer, dict):
            words = answer.get("error") if isinstance(answer, dict) else None
            self._trouble(f"The room refused the check-in ({status}): {words or 'no reason given'}")
            return None
        state = answer.get("state")
        self._trouble(None if state == "allowed" else "Waiting to join: tap Allow in Settings › Software on the room page")
        if state != "allowed":
            return answer
        if self.trial:
            self._confirm()
            return answer
        job = answer.get("job")
        if isinstance(job, dict):
            self.take(job)
        return answer

    def network(self) -> Dict[str, Any]:
        """How the touch Pi is connected, for the TV Pi's Network page: the address it reaches the room from, and its Wi-Fi (read at
        most every STATUS_EVERY_S; at once after a Wi-Fi job)."""
        network = dict(self.host.network(self.url))
        now = self.host.monotonic()
        if self._wifi_status is None or now - self._wifi_status_at >= STATUS_EVERY_S:
            self._wifi_status_at = now
            try:
                status = self.wifi.status()
            except Exception as e:  # noqa: BLE001 - no NetworkManager, or it did not answer: the check-in goes on without it
                logger.debug(f"Could not read the Wi-Fi status: {e}")
                status = {}
            change = status.get("change") or None
            self._wifi_status = {key: status[key] for key in ("wifi", "connection", "ssid", "signal") if key in status}
            if status:
                self._wifi_status["saved"] = [s[:32] for s in status.get("saved", [])][:20]
                self._wifi_status["change"] = dict(change, words=str(change.get("words") or "")[:200]) if change else None
        for key, value in (self._wifi_status or {}).items():
            network.setdefault(key, value)
        return network

    def _trouble(self, words: Optional[str]) -> None:
        if words != self._said:
            if words:
                logger.warning(words)
            elif self._said:
                logger.info("Checking in with the room")
            self._said = words

    def _confirm(self) -> None:
        """The new version has checked in: tell the good agent, which is waiting for this (supervise)."""
        if self.paths.trial_ok.exists():
            return
        try:
            fd = os.open(self.paths.trial_ok, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.write(fd, self.version.encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
            logger.info(f"Version {short(self.version)} checked in with the room")
        except OSError as e:
            logger.error(f"Could not say that this version checked in: {e}")

    def loop(self) -> None:
        while not self.stopping:
            self.checkin()
            self.host.sleep(INTERVAL_S)

    # --- jobs

    def take(self, job: Dict[str, Any]) -> bool:
        """Start a job the TV Pi handed out, unless it is one this agent has had already or another is running. True when started."""
        job_id, kind, args = job.get("id"), job.get("type"), job.get("args")
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id) or kind not in JOBS:
            return False
        with self._lock:
            if self._job.get("id") == job_id or (self._worker is not None and self._worker.is_alive()):
                return False
        self._set_job(id=job_id, type=kind, state="running", words="Starting", result=None)
        args = args if isinstance(args, dict) else {}
        worker = threading.Thread(target=self._do, args=(job_id, kind, args), name=f"job-{kind}", daemon=True)
        with self._lock:
            self._worker = worker
        worker.start()
        return True

    def wait(self, timeout: Optional[float] = None) -> None:
        """Until the job that runs has ended (the tests)."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def _do(self, job_id: str, kind: str, args: Dict[str, Any]) -> None:
        try:
            words = JOBS[kind](self, job_id, args)
        except Failed as e:
            self._set_job(state="failed", words=str(e))
            logger.warning(f"{kind}: {e}")
            return
        except Exception as e:   # noqa: BLE001 - a job that breaks says so, and the agent goes on
            logger.exception(f"{kind} broke")
            self._set_job(state="failed", words=f"It stopped with an error: {e}")
            return
        if words is not None:
            self._set_job(state="done", words=words)

    def progress(self, words: str) -> None:
        logger.info(words)
        self._set_job(words=words)

    def do_reboot(self, job_id: str, args: Dict[str, Any]) -> Optional[str]:
        self.progress("Rebooting")
        self.host.sync()
        code, out = self.host.run(["systemctl", "reboot"])
        if code != 0:
            raise Failed(f"Could not reboot: {last_line(out) or code}")
        return None   # "Rebooted" is told after it (_recover_job)

    # --- Wi-Fi

    def do_wifi_scan(self, job_id: str, args: Dict[str, Any]) -> Optional[str]:
        self.progress("Looking for networks")
        try:
            found = self.wifi.scan(fresh=True)["networks"]
        except nmcli.WifiError as e:
            raise Failed(str(e)) from e
        finally:
            self._wifi_status = None
        networks = [{key: n.get(key) for key in ("ssid", "signal", "security", "open", "saved", "in_use")} for n in found][:30]
        self._result_until = self.host.monotonic() + RESULT_FOR_S
        self._set_job(result={"networks": networks})
        return f"Found {len(networks)} network{'' if len(networks) == 1 else 's'}"

    def do_wifi_connect(self, job_id: str, args: Dict[str, Any]) -> Optional[str]:
        hidden = args.get("hidden", False)
        if not isinstance(hidden, bool):
            raise Failed("Hidden must be true or false")
        try:
            plan = self.wifi.prepare(args.get("ssid"), args.get("password") or "", hidden)
            return self.wifi.change(plan, lambda device, since: self._wifi_checks(device, plan.ethernet), self.progress, job=job_id)
        except nmcli.WifiError as e:
            raise Failed(str(e)) from e
        finally:
            self._wifi_status = None

    def _wifi_checks(self, device: str, ethernet: bool):
        """The touch Pi's job on a new network: it reaches the room (GET /api/status). On Ethernet the room is reached that way
        whatever the Wi-Fi does, so the Wi-Fi's gateway must answer too."""
        reaches = nmcli.Check(lambda: self.host.room_status(self.url),
                              f"the touch Pi could not reach the room at {self.url} within {nmcli.TRIAL_S} seconds")
        if ethernet:
            return [self.wifi.gateway_check(device), reaches], "The touch Pi is on Ethernet, so its Wi-Fi gateway was checked as well"
        return [reaches], "The touch Pi reaches the room"

    def do_wifi_forget(self, job_id: str, args: Dict[str, Any]) -> Optional[str]:
        try:
            return self.wifi.forget(args.get("ssid"))
        except nmcli.WifiError as e:
            raise Failed(str(e)) from e
        finally:
            self._wifi_status = None

    def _recover_wifi(self) -> None:
        """A Wi-Fi change the agent's stop cut short (a power cut, a crash): finished when it was proven, undone when not."""
        try:
            words = self.wifi.recover()
        except Exception:  # noqa: BLE001 - the agent must start all the same
            logger.exception("Could not finish or undo the Wi-Fi change")
            return
        if words:
            logger.warning(f"Wi-Fi: {words}")

    # --- update

    def _record(self, **fields: Any) -> Dict[str, Any]:
        record = dict(read_json(self.paths.update), **fields)
        write_json(self.paths.update, record)
        return record

    def do_update(self, job_id: str, args: Dict[str, Any]) -> Optional[str]:
        commit = args.get("commit")
        if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
            raise Failed("Send the version to install (a commit)")
        if commit == self.version:
            raise Failed("This touch Pi runs that version already")
        running = self.version
        if not COMMIT.fullmatch(running) or link_target(self.paths.good) != running or link_target(self.paths.current) != running:
            raise Failed("This touch Pi was not set up for updates: run install-kiosk.sh on it once more")
        record = {"state": "building", "commit": commit, "from": running, "job": job_id, "at": now_iso(), "trials": 0}
        write_json(self.paths.update, record)
        try:
            self.progress("Fetching main from GitHub")
            code, out = self.host.run(["git", "-C", str(self.paths.repo), "fetch", "--quiet", "origin",
                                       f"+refs/heads/main:{MAIN}"], timeout=LONG_TIMEOUT_S)
            if code != 0:
                raise Failed(f"Can't reach GitHub ({last_line(out) or code})")
            code, _ = self.host.run(["git", "-C", str(self.paths.repo), "merge-base", "--is-ancestor", commit, MAIN])
            if code != 0:
                raise Failed("That version is not on main on GitHub")
            if self.trusted(commit):
                self.progress(f"{short(commit)} is on this touch Pi already")
            else:
                self.build(commit)
        except Failed as e:
            self._remove_partial(commit)
            self._close(record, "failed", str(e))
            return None
        # From here the running release is no longer the only one in use: every step is written down first
        self._record(state="switching")
        point(self.paths.current, commit)
        self.progress(f"Setting up {short(commit)}")
        code, out = self.setup(commit)
        if code != 0:
            why = f"its kiosk setup did not finish ({last_line(out) or code})"
            self._record(state="rolling_back", why=why)
            point(self.paths.current, running)
            self.setup(running)
            self._close(record, "failed", why)
            return None
        self._record(state="trial", trials=0)
        self.progress(f"Restarting on {short(commit)}")
        self.host.sync()
        code, out = self.host.run(["systemctl", "reboot"])
        if code != 0:
            # Not rebooted: the agent that starts at the next boot proves it (or goes back) all the same
            raise Failed(f"Could not reboot onto {short(commit)}: {last_line(out) or code}")
        return None

    def build(self, commit: str) -> None:
        """Put the commit's files in releases/<commit>: unpacked into a folder of its own, made root's and read-only to the others,
        flushed, VERSION written last, then renamed into place. Nothing of the running release is touched."""
        self.progress(f"Getting {short(commit)}")
        partial = self.paths.releases / f".partial-{commit}"
        tarball = self.paths.base / f".{commit}.tar"
        self._remove_partial(commit)
        partial.mkdir(parents=True, mode=0o755)
        code, out = self.host.run(["git", "-C", str(self.paths.repo), "archive", "--format=tar", "-o", str(tarball), commit])
        if code != 0:
            raise Failed(f"Could not get its files ({last_line(out) or code})")
        code, out = self.host.run(["tar", "-xf", str(tarball), "-C", str(partial), "--no-same-owner", "--no-same-permissions"])
        with _suppress_missing():
            os.unlink(tarball)
        if code != 0:
            raise Failed(f"Could not unpack its files ({last_line(out) or code})")
        seal(partial)
        self.host.sync()
        version = partial / "VERSION"
        version.write_text(commit + "\n")
        os.chmod(version, 0o644)
        target = self.paths.release(commit)
        if target.exists() or target.is_symlink():
            remove_tree(target)
        os.rename(partial, target)
        sync_dir(self.paths.releases)

    def _remove_partial(self, commit: str) -> None:
        remove_tree(self.paths.releases / f".partial-{commit}")
        with _suppress_missing():
            os.unlink(self.paths.base / f".{commit}.tar")

    def trusted(self, release: Optional[str]) -> bool:
        """A whole release this agent may run: VERSION names it, and its folder, VERSION, kiosk setup and agent are the agent's own
        (root's) and not writable by anyone else."""
        try:
            folder = self.paths.release(release or "")
        except ValueError:
            return False
        try:
            if folder.is_symlink() or not folder.is_dir() or (folder / "VERSION").read_text().strip() != release:
                return False
            for path in (folder, folder / "VERSION", folder / SETUP_FILE, folder / AGENT_FILE):
                info = os.lstat(path)
                if info.st_uid != self.host.euid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or stat.S_ISLNK(info.st_mode):
                    return False
        except OSError:
            return False
        return True

    def setup(self, release: str) -> Tuple[int, str]:
        """Run the release's kiosk setup, as the installer does (install-kiosk.sh --url, for the desktop user), in its agent mode: it
        writes the kiosk command, the autostart, the icon and this agent's unit, and leaves the agent running."""
        if not self.trusted(release):
            return 1, f"{short(release)} is not whole on this touch Pi"
        env = {"PATH": SAFE_PATH, "HOME": "/root", "LANG": "C.UTF-8", "KIOSK_USER": self.user, "KIOSK_FROM_AGENT": "1"}
        return self.host.run(["bash", str(self.paths.release(release) / SETUP_FILE), "--url", self.url], env=env,
                             timeout=LONG_TIMEOUT_S)

    def _close(self, record: Dict[str, Any], state: str, why: str) -> None:
        """The update has ended (done, failed, rolled_back): its record, then the job's result for the TV Pi (_tell)."""
        self._tell(self._record(state=state, why=why, ended=now_iso()))

    def _tell(self, record: Dict[str, Any]) -> None:
        """The result of the update `record` ended with, as its job's state, when that job is the agent's last one."""
        commit, came_from, why = record.get("commit"), record.get("from"), record.get("why")
        if record.get("state") == "done":
            words, result = f"Updated to {short(commit)}", "done"
        elif record.get("state") == "rolled_back":
            words, result = f"The update to {short(commit)} was undone: {why}. It is back on {short(came_from)}", "failed"
        else:
            words, result = f"The update to {short(commit)} did not happen: {why}. The touch Pi is still on {short(came_from)}", "failed"
        job_id = record.get("job")
        if not JOB_ID.fullmatch(str(job_id or "")):
            return
        with self._lock:
            if self._job.get("id") not in (None, job_id):
                return   # the agent has had another job since
            self._job = dict(self._job, id=job_id, type="update")
        self._set_job(state=result, words=words)

    # --- after a start: what an update left to do

    def recover(self) -> Optional[str]:
        """Finish or undo what an update left when the agent stopped (a reboot, a power cut, a crash): see the module's docstring."""
        record = read_json(self.paths.update)
        state = record.get("state")
        if state not in ("building", "switching", "trial", "rolling_back"):
            return None
        commit, came_from = record.get("commit"), record.get("from")
        if not (isinstance(commit, str) and COMMIT.fullmatch(commit) and isinstance(came_from, str) and COMMIT.fullmatch(came_from)):
            write_json(self.paths.update, {"state": "failed", "why": "The update's record was not whole", "ended": now_iso()})
            return None
        if state == "building":
            self._remove_partial(commit)
            self._close(record, "failed", "it was cut off before it was finished")
            return None
        if state == "trial":
            if link_target(self.paths.good) == commit:      # proven, and cut off as it was being made the good one
                self._close(record, "done", "")
                return None
            if link_target(self.paths.current) == commit and self.trusted(commit):
                return "trial"
            return self.go_back(record, "it was cut off before it was finished")
        # switching (cut off between the record and the reboot), or rolling_back (cut off going back): back to the release before
        return self.go_back(record, record.get("why") or "it was cut off before it was finished")

    def go_back(self, record: Dict[str, Any], why: str) -> str:
        """Back to the release that ran before the update: `current` to it, its kiosk setup again, the record, and a reboot onto it.
        Every step may be done again: a start in "rolling_back" does them all once more."""
        came_from = record["from"]
        self._record(state="rolling_back", why=why)
        if self.trusted(came_from):
            point(self.paths.current, came_from)
            code, out = self.setup(came_from)
            if code != 0:
                logger.error(f"The kiosk setup of {short(came_from)} did not finish while going back: {last_line(out)}")
        else:
            logger.error(f"Can't go back to {short(came_from)}: it is not whole on this touch Pi")
        self._close(record, "rolled_back", why)
        self.host.sync()
        logger.warning(f"The update to {short(record['commit'])} was undone ({why}): rebooting onto {short(came_from)}")
        self.host.run(["systemctl", "reboot"])
        return "rebooting"

    def supervise(self) -> str:
        """The good agent, after the reboot into a new version: start that version's agent (--trial) and wait for its word that it
        checked in with the TV Pi. With it within TRIAL_S, the new version becomes the good one and the agent starts over from it
        ("promoted"); without it, back to the version before ("rebooting"). The minutes are counted from this start: the clock of a
        Pi without a battery is not to be trusted at boot, and a start that was cut off counts toward MAX_TRIALS."""
        record = self._record(trials=int(read_json(self.paths.update).get("trials") or 0) + 1)
        commit = record["commit"]
        if record["trials"] > MAX_TRIALS:
            return self.go_back(record, f"the touch Pi did not check in with it after {MAX_TRIALS} starts")
        with _suppress_missing():
            os.unlink(self.paths.trial_ok)
        logger.info(f"Starting {short(commit)} to see that it checks in with the room")
        deadline = self.host.monotonic() + TRIAL_S
        child, started = None, None
        try:
            while True:
                if self._proven(commit):
                    self._stop(child)
                    child = None
                    return self.promote(record)
                now = self.host.monotonic()
                if now >= deadline:
                    self._stop(child)
                    child = None
                    return self.go_back(record, "the touch Pi did not check in with it within 5 minutes")
                if (child is None or child.poll() is not None) and (started is None or now - started >= CHILD_RESTART_S):
                    started = now
                    child = self.host.spawn([self.host.python, str(self.paths.agent(commit)), "--trial", "--base", str(self.paths.base),
                                             "--state", str(self.paths.state)])
                self.host.sleep(1)
        finally:
            self._stop(child)

    def _proven(self, commit: str) -> bool:
        try:
            return self.paths.trial_ok.read_text().strip() == commit
        except OSError:
            return False

    @staticmethod
    def _stop(child) -> None:
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(5)

    def promote(self, record: Dict[str, Any]) -> str:
        """The new version checked in: it is the good one now. Then the agent starts over from it (the same process, its code)."""
        commit = record["commit"]
        point(self.paths.good, commit)
        self._close(record, "done", "")
        with _suppress_missing():
            os.unlink(self.paths.trial_ok)
        self.host.sync()
        logger.info(f"Updated to {short(commit)}")
        self.host.execv(self.host.python, [self.host.python, str(self.paths.good / AGENT_FILE), "--base", str(self.paths.base),
                                           "--state", str(self.paths.state)])
        return "promoted"

    def _recover_job(self) -> None:
        """A job that was running when the agent stopped: a reboot has happened; an update has its record's word (written by _close);
        anything else did not finish."""
        job = self._job
        if job.get("state") != "running":
            return
        record = read_json(self.paths.update)
        wifi = self.wifi.record()
        if job.get("type") == "reboot":
            self._set_job(state="done", words="Rebooted")
        elif job.get("type") == "wifi_connect" and wifi.get("job") == job.get("id") and wifi.get("state") in ("done", "undone"):
            self._set_job(state="done" if wifi["state"] == "done" else "failed", words=wifi.get("words") or "")
        elif job.get("type") == "update" and record.get("job") == job.get("id") and record.get("state") in ("done", "failed", "rolled_back"):
            self._tell(record)     # the update ended, and the agent stopped before its job said so
        else:
            self._set_job(state="failed", words="The touch Pi restarted before it finished")

    def sweep(self) -> None:
        """Remove what an update left behind (a half-built folder, its archive, a link half made) and the releases no longer kept: the
        good one, the current one and the one before an update are kept."""
        record = read_json(self.paths.update)
        keep = {link_target(self.paths.good), link_target(self.paths.current), record.get("from"), record.get("commit")}
        if self.paths.releases.is_dir():
            for entry in self.paths.releases.iterdir():
                if entry.name.startswith(".partial-") or (COMMIT.fullmatch(entry.name) and entry.name not in keep):
                    remove_tree(entry)
        if self.paths.base.is_dir():
            for entry in self.paths.base.iterdir():
                if (entry.name.startswith(".") and entry.name.endswith(".tar")) or entry.name.startswith((".current-", ".good-")):
                    remove_tree(entry)


def remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        with _suppress_missing():
            os.unlink(path)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def seal(folder: Path) -> None:
    """No write for group or others anywhere in the folder (links are left as they are)."""
    for root, dirs, files in os.walk(folder):
        for name in [*dirs, *files, None]:
            path = os.path.join(root, name) if name is not None else root
            info = os.lstat(path)
            if not stat.S_ISLNK(info.st_mode) and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                os.chmod(path, stat.S_IMODE(info.st_mode) & ~(stat.S_IWGRP | stat.S_IWOTH))


JOBS: Dict[str, Callable[[Agent, str, Dict[str, Any]], Optional[str]]] = {
    "reboot": Agent.do_reboot,
    "update": Agent.do_update,
    "wifi_scan": Agent.do_wifi_scan,
    "wifi_connect": Agent.do_wifi_connect,
    "wifi_forget": Agent.do_wifi_forget,
}


def running_release(here: Path) -> str:
    """The release this file is in (releases/<commit>/tools/kiosk_agent/…), or "unknown" (a copy run from a clone)."""
    folder = here.resolve().parents[2]
    return folder.name if COMMIT.fullmatch(folder.name) and folder.parent.name == "releases" else "unknown"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="The touch Pi's agent: checks in with the room's TV Pi and does its jobs")
    parser.add_argument("--trial", action="store_true", help="prove a new version (started by the good agent after an update)")
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--state", default=STATE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    os.umask(0o022)
    paths = Paths(args.base, args.state)
    agent = Agent(paths, Host(), running_release(Path(__file__)), trial=args.trial)
    if not agent.url:
        logger.error(f"No room address in {paths.config}: run installer/install-kiosk.sh --url http://<room device>:8080/")
        time.sleep(60)
        return 1
    try:
        plan = agent.start()
    except OSError as e:
        logger.error(f"Could not start: {e}")
        time.sleep(30)
        return 1
    if plan == "rebooting":
        time.sleep(300)
        return 0
    if plan == "trial":
        agent.supervise()
        time.sleep(300)    # rebooting (promoted execs and does not come back here)
        return 0
    logger.info(f"Checking in with {agent.url} as {agent.identity['id']}, version {short(agent.version)}")
    agent.loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
