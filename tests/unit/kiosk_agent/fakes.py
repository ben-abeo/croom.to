"""
Stand-ins for the touch Pi agent's machine: a touch Pi's folders in a temporary one (releases, links, state), a command runner that plays
git, tar, bash (the kiosk setup) and systemctl, a room that answers check-ins with the real registry, a clock, and the trial's child
process. Nothing runs as root, reboots, or reaches a network.
"""

import importlib.util
import io
import json
import os
import tarfile
from pathlib import Path

from croom.settings.checkin import CheckinError, Registry
from tests.unit.admin.fake_nm import FakeNM

REPO = Path(__file__).resolve().parents[3]
AGENT_PATH = REPO / "tools" / "kiosk_agent" / "croom_kiosk_agent.py"


def load_agent():
    spec = importlib.util.spec_from_file_location("croom_kiosk_agent", AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = load_agent()

A = "a" * 40
B = "b" * 40
C = "c" * 40
URL = "http://pimeet-3.local:8080/"
USER = "pi"


class PowerCut(BaseException):
    """The power goes: nothing after it happens (not an Exception, so that no `except Exception` of the agent carries on after it)."""


class Clock:
    """Wall clock and monotonic clock in one; sleep moves both on."""

    def __init__(self):
        self.now = 1_000.0
        self.slept = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds


def release(paths, name, mode=0o644):
    """A whole release as the agent or the installer leaves it."""
    folder = paths.releases / name
    (folder / "installer").mkdir(parents=True, exist_ok=True)
    (folder / "tools" / "kiosk_agent").mkdir(parents=True, exist_ok=True)
    (folder / "installer" / "install-kiosk.sh").write_text(f"# kiosk setup of {name}\n")
    (folder / "tools" / "kiosk_agent" / "croom_kiosk_agent.py").write_text(f"# agent of {name}\n")
    (folder / "VERSION").write_text(name + "\n")
    for path in (folder / "installer" / "install-kiosk.sh", folder / "tools" / "kiosk_agent" / "croom_kiosk_agent.py", folder / "VERSION"):
        os.chmod(path, mode)
    for path in (folder, folder / "installer", folder / "tools", folder / "tools" / "kiosk_agent"):
        os.chmod(path, 0o755)
    return folder


def files_of(name):
    """The files `git archive` gives for a commit (group-writable, as git's default umask leaves them)."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        for path, text in (("installer/install-kiosk.sh", f"# kiosk setup of {name}\n"),
                           ("tools/kiosk_agent/croom_kiosk_agent.py", f"# agent of {name}\n"), ("README.md", "readme\n")):
            raw = text.encode()
            info = tarfile.TarInfo(path)
            info.size = len(raw)
            info.mode = 0o664
            tar.addfile(info, io.BytesIO(raw))
    return data.getvalue()


class Runner:
    """Plays the commands: git fetch (fails when `offline`), git merge-base --is-ancestor (main is `main`: the commits on it), git
    archive, tar -xf, bash <release>/installer/install-kiosk.sh (fails for the releases in `bad_setup`) and systemctl reboot. `calls`
    lists every command; `setups` the releases set up, in order; `reboots` counts."""

    def __init__(self, main=(A, B, C), nm=None):
        self.main = set(main)
        self.nm = nm                    # NetworkManager (nmcli, and ping): its own stand-in, which keeps its own calls
        self.offline = False
        self.bad_setup = set()
        self.reboot_fails = False
        self.calls, self.setups, self.envs = [], [], []
        self.reboots = 0
        self.cut = None                 # a callable run before each command (PowerCut counts them)

    def __call__(self, args, env=None, timeout=None):
        if self.nm is not None and (args[0] in ("nmcli", "ping") or (args[0] == "env" and args[3:4] and args[3] in ("nmcli", "ping"))):
            return self.nm(args)
        if self.cut:
            self.cut()
        self.calls.append(list(args))
        if args[:1] == ["git"] and "fetch" in args:
            return (128, "fatal: unable to access 'https://github.com/': Could not resolve host") if self.offline else (0, "")
        if args[:1] == ["git"] and "merge-base" in args:
            return (0, "") if args[-2] in self.main else (1, "")
        if args[:1] == ["git"] and "archive" in args:
            Path(args[args.index("-o") + 1]).write_bytes(files_of(args[-1]))
            return 0, ""
        if args[:2] == ["tar", "-xf"]:
            with tarfile.open(args[2]) as tar:
                tar.extractall(args[args.index("-C") + 1], filter="fully_trusted")   # git's modes kept: the seal must clear them
            return 0, ""
        if args[:1] == ["bash"]:
            name = Path(args[1]).parents[1].name
            self.setups.append(name)
            self.envs.append(env)
            return (1, f"setup of {name} broke\nE: no space left") if name in self.bad_setup else (0, "set up")
        if args == ["systemctl", "reboot"]:
            self.reboots += 1
            return (1, "Failed to reboot") if self.reboot_fails else (0, "")
        raise AssertionError(f"unexpected command {args}")


class Room:
    """The TV Pi's check-in route, with the real registry: post() as the agent's HTTP; `down` gives no answer."""

    def __init__(self, clock):
        self.registry = Registry(None, clock=clock)
        self.down = False
        self.posts = []

    def __call__(self, url, body, headers):
        if self.down:
            raise OSError("Connection refused")
        data = json.loads(body)
        self.posts.append((url, data, dict(headers)))
        try:
            answer = self.registry.checkin(data, headers.get("X-Croom-Device-Token", ""), "10.0.0.9")
        except CheckinError as e:
            return e.status, json.dumps({"error": str(e)}).encode()
        return 200, json.dumps(answer).encode()


class Child:
    """The trial's agent process. `behaviour` is called at each poll with the child: it may write trial-ok (it checked in) or end."""

    def __init__(self, args, behaviour):
        self.args = args
        self.behaviour = behaviour
        self.code = None
        self.polls = 0
        self.terminated = False

    def poll(self):
        self.polls += 1
        if self.code is None:
            self.behaviour(self)
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code


class Device:
    """A touch Pi set up by install-kiosk.sh at release A: `agent()` makes an agent as systemd would start it (from `good`)."""

    def __init__(self, root, running=A):
        self.paths = agent.Paths(str(root / "opt"), str(root / "state"))
        self.paths.releases.mkdir(parents=True)
        self.paths.state.mkdir(parents=True)
        release(self.paths, running)
        agent.point(self.paths.current, running)
        agent.point(self.paths.good, running)
        (self.paths.config).write_text(json.dumps({"url": URL, "user": USER}))
        self.clock = Clock()
        (root / "nm").mkdir()
        self.nm = FakeNM(root / "nm")
        self.runner = Runner(nm=self.nm)
        self.room_up = lambda url: True      # GET <room>/api/status answers (on whichever network the touch Pi is)
        self.room = Room(self.clock)
        self.children = []
        self.child_does = lambda child: None
        self.execs = []
        self.syncs = 0

    def spawn(self, args):
        child = Child(args, self.child_does)
        self.children.append(child)
        return child

    def sync(self):
        self.syncs += 1

    def host(self):
        return agent.Host(run=self.runner, post=self.room, clock=self.clock, monotonic=self.clock, sleep=self.clock.sleep,
                          spawn=self.spawn, execv=lambda path, args: self.execs.append(args), sync=self.sync, python="/usr/bin/python3",
                          network=lambda url: {"address": "10.0.0.9"}, hostname=lambda: "touch-pi",
                          room_status=lambda url: self.room_up(url), nm_connections=str(self.nm.dir))

    def running(self):
        return agent.link_target(self.paths.good)

    def agent(self, version=None, trial=False):
        return agent.Agent(self.paths, self.host(), version or self.running(), trial=trial)

    def record(self):
        return json.loads(self.paths.update.read_text()) if self.paths.update.exists() else {}

    def job(self):
        return json.loads(self.paths.job.read_text()) if self.paths.job.exists() else {}

    def links(self):
        return agent.link_target(self.paths.current), agent.link_target(self.paths.good)
