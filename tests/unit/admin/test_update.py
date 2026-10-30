"""The TV Pi's updates (spec 2026-10-29, section 6) against a fake machine: a temporary folder stands in for /opt/croom (releases/, the
venv link, VERSION), a fake runner for python -m venv, pip, playwright and systemctl, a fake probe for GET /api/status, a fake clock
whose sleep moves it, and a fake GitHub that says whether a commit is on main. Nothing needs root, the network or GitHub."""

import getpass
import grp
import json
import os
import stat

import pytest

from croom.admin.jobs import HEALTH_INTERVAL_S, HEALTH_TIMEOUT_S, UPDATE_UNDER_WAY, Host, JobError, Jobs, Paths
from croom.admin.update import NEEDS_INSTALLER, REPO_URL, UPDATE_TIMEOUT_S
from croom.settings.github import CANT_CHECK, GitHubError

A = "a" * 40          # the running release
B = "b" * 40          # the newest on main
P = "c" * 40          # the release kept before A
GOOD = "room:\n  name: Conference A\n"


class Runner:
    """The commands an update runs, as a machine would answer them: `python -m venv DIR` makes DIR/bin; `fail` maps a word of a
    command (pip, playwright, -c, venv) to (code, output) for it to fail with; `calls` is every command, in order."""

    def __init__(self):
        self.calls = []
        self.fail = {}
        self.answers = {}      # the start of a command -> (code, output), or a function giving them
        self.on_call = None

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if self.on_call:
            self.on_call(args)
        for word, answer in self.fail.items():
            if any(word == a or a.endswith("/" + word) for a in args):
                return answer
        if args[1:3] == ["-m", "venv"]:
            os.makedirs(os.path.join(args[3], "bin"), exist_ok=True)
            with open(os.path.join(args[3], "bin", "python"), "w"):
                pass
        if args[0].endswith("/pip"):          # pip under a umask that leaves what it makes writable by the group and others
            path = os.path.join(os.path.dirname(os.path.dirname(args[0])), "lib", "croom.py")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w"):
                pass
            os.chmod(path, 0o666)
            os.chmod(os.path.dirname(path), 0o777)
        for word, answer in self.answers.items():
            if " ".join(args).startswith(word):
                return answer() if callable(answer) else answer
        return 0, ""

    def systemctl(self):
        return [c for c in self.calls if c[0] == "systemctl"]


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Probe:
    def __init__(self, up=True):
        self.up = up
        self.asked = 0

    def __call__(self, host, port):
        self.asked += 1
        return self.up(self.asked) if callable(self.up) else self.up


class FakeGitHub:
    def __init__(self, on_main=True):
        self.on_main = on_main
        self.asked = []

    def on_branch(self, commit):
        self.asked.append(commit)
        if isinstance(self.on_main, Exception):
            raise self.on_main
        return self.on_main


@pytest.fixture
def paths(tmp_path):
    p = Paths(config_dir=tmp_path / "etc", data_dir=tmp_path / "var", media_root=tmp_path / "media", install_dir=tmp_path / "opt")
    for folder in (p.config_dir, p.data_dir, p.media_root, p.install_dir):
        folder.mkdir()
    p.config.write_text(GOOD)
    return p


def release(paths, name, complete=True):
    """A release folder as the installer or an update leaves it."""
    folder = paths.releases / name
    (folder / "venv" / "bin").mkdir(parents=True)
    (folder / "venv" / "bin" / "python").write_text("")
    if complete:
        (folder / "VERSION").write_text(name + "\n")
    return folder


def link(paths, name):
    paths.venv_link.unlink(missing_ok=True)
    os.symlink(os.path.join("releases", name, "venv"), paths.venv_link)


@pytest.fixture
def layout(paths):
    """A TV Pi on release A, with P kept before it."""
    release(paths, A)
    release(paths, P)
    link(paths, A)
    write_state(paths, {"previous_release": P})
    return paths


def write_state(paths, state):
    """The helper's own record, as the helper keeps it: its folder mode 700, the file mode 600."""
    paths.admin_dir.mkdir(mode=0o700, exist_ok=True)
    paths.state.write_text(json.dumps(state))
    paths.state.chmod(0o600)


def read_state(paths):
    return json.loads(paths.state.read_text())


class Machine:
    def __init__(self, paths, probe=None, github=None, background=None):
        self.paths = paths
        self.runner, self.clock = Runner(), Clock()
        self.probe = probe or Probe()
        self.github = github or FakeGitHub()
        self.queued = []
        self.busy = False          # whether the room says a meeting is on (GET /api/devices/busy): a value, or a function of the clock
        self.asked = []            # what the room was asked on the loopback
        self.jobs = Jobs(Host(paths=paths, run=self.runner, run_long=self.runner, owner=getpass.getuser(),
                              group=grp.getgrgid(os.getgid()).gr_name, probe=self.probe, clock=self.clock, sleep=self.clock.sleep,
                              monotonic=self.clock, background=background or (lambda work: work()), github=self.github,
                              python="/usr/bin/python3", sync=self.synced, ask_room=self.ask_room))
        self.syncs = []

    def ask_room(self, host, port, path):
        self.asked.append((host, port, path))
        if path != "/api/devices/busy":
            return None
        return {"busy": self.busy(self.clock()) if callable(self.busy) else self.busy}

    def synced(self):
        self.syncs.append(sorted(p.name for p in self.paths.releases.iterdir() if (p / "VERSION").exists()))

    def job(self, name, **args):
        return self.jobs.table[name](args)

    def record(self):
        return self.job("update_status")

    def running(self):
        return os.readlink(self.paths.venv_link)

    def state(self):
        return read_state(self.paths)


def releases(paths):
    return sorted(p.name for p in paths.releases.iterdir())


# --- an update that works


def test_an_update_builds_beside_the_running_release_switches_restarts_and_watches(layout):
    m = Machine(layout)
    assert m.job("update", commit=B) == {"updating": True}
    assert m.github.asked == [B]                                     # only a commit of main is installed
    venv = str(layout.releases / B / "venv")
    assert m.runner.calls[:5] == [
        ["/usr/bin/python3", "-m", "venv", venv],
        [venv + "/bin/pip", "install", "--no-input", "--disable-pip-version-check", f"git+{REPO_URL}@{B}"],
        ["runuser", "-u", getpass.getuser(), "--", "env", f"PLAYWRIGHT_BROWSERS_PATH={layout.install_dir / 'browsers'}",
         venv + "/bin/playwright", "install", "chromium"],
        [venv + "/bin/python", "-c", "import croom.core.agent, croom.admin.jobs"],
        [venv + "/bin/python", "-m", "croom.admin", "--check"],          # the new release's helper starts, before it is switched to
    ]
    assert m.runner.systemctl() == [["systemctl", "restart", "croom"], ["systemctl", "--no-block", "restart", "croom-admin"]]
    assert m.running() == f"releases/{B}/venv"                       # a relative link, made beside the old one and renamed over it
    assert (layout.releases / B / "VERSION").read_text() == B + "\n"
    assert layout.version_file.read_text() == B + "\n"
    assert stat.S_IMODE(os.stat(layout.releases / B).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(layout.releases / B / "VERSION").st_mode) == 0o644
    record = m.record()
    assert record["state"] == "done" and record["commit"] == B and record["from"] == A
    assert record["lines"] == [
        "Making a new environment for bbbbbbb",
        "Installing bbbbbbb from GitHub: this takes several minutes",
        "Installing the meeting browser it uses",
        "Checking that it starts",
        "Checking that its helper starts",
        "Switched to bbbbbbb",
        "Restarting the room software",
        "Waiting for the room to answer (up to 3 minutes)",
        "The room is running bbbbbbb",
    ]
    assert m.job("version")["version"] == B and m.job("version")["previous"] == A
    assert not [p for p in layout.install_dir.iterdir() if p.name.startswith(".venv-")]


def test_two_releases_are_kept_the_running_one_and_the_one_before(layout):
    m = Machine(layout)
    m.job("update", commit=B)
    assert releases(layout) == [A, B]                                # P, the one before A, is gone
    assert m.state()["previous_release"] == A


def test_the_progress_lines_are_in_the_record_while_it_builds(layout):
    seen = []
    m = Machine(layout)
    m.runner.on_call = lambda args: seen.append((m.record()["state"], list(m.record()["lines"])))
    m.job("update", commit=B)
    assert seen[0] == ("building", ["Making a new environment for bbbbbbb"])
    assert seen[1] == ("building", ["Making a new environment for bbbbbbb", "Installing bbbbbbb from GitHub: this takes several minutes"])
    assert ("restarting", ["Making a new environment for bbbbbbb", "Installing bbbbbbb from GitHub: this takes several minutes",
                           "Installing the meeting browser it uses", "Checking that it starts", "Checking that its helper starts",
                           "Switched to bbbbbbb", "Restarting the room software"]) in seen


def test_the_link_is_switched_by_renaming_a_new_link_over_it(layout, monkeypatch):
    m = Machine(layout)
    replaced = []
    real = os.replace

    def watch(source, target):
        if str(target) == str(layout.venv_link):
            replaced.append((os.path.islink(source), os.readlink(source)))
        return real(source, target)
    monkeypatch.setattr(os, "replace", watch)
    m.job("update", commit=B)
    assert replaced == [(True, f"releases/{B}/venv")]


def test_a_release_already_built_whole_is_not_built_again(layout):
    release(layout, B)
    m = Machine(layout)
    m.job("update", commit=B)
    assert not [c for c in m.runner.calls if "pip" in " ".join(c)]
    assert m.record()["lines"][0] == "bbbbbbb is on this TV Pi already"
    assert m.record()["state"] == "done"


# --- an update that does not work


def test_a_build_that_fails_leaves_the_running_release_and_removes_its_folder(layout):
    m = Machine(layout)
    m.runner.fail["pip"] = (1, "Collecting croom\nERROR: Could not find a version that satisfies the requirement evdev\n")
    m.job("update", commit=B)
    assert m.running() == f"releases/{A}/venv"
    assert releases(layout) == [A, P]
    assert m.runner.systemctl() == []                                # the room was never touched
    record = m.record()
    assert record["state"] == "failed"
    assert record["why"] == "Collecting croom ERROR: Could not find a version that satisfies the requirement evdev"
    assert record["lines"][-1] == "Installing bbbbbbb from GitHub failed: the room is still on aaaaaaa"


@pytest.mark.parametrize("word, line", [
    ("venv", "Making a new environment for bbbbbbb failed: the room is still on aaaaaaa"),
    ("playwright", "Installing the meeting browser it uses failed: the room is still on aaaaaaa"),
    ("-c", "Checking that it starts failed: the room is still on aaaaaaa"),
    ("--check", "Checking that its helper starts failed: the room is still on aaaaaaa"),
])
def test_each_step_that_fails_stops_the_update_before_the_switch(layout, word, line):
    m = Machine(layout)
    m.runner.fail[word] = (1, "it went wrong")
    m.job("update", commit=B)
    assert m.running() == f"releases/{A}/venv" and releases(layout) == [A, P]
    assert m.record()["state"] == "failed" and m.record()["lines"][-1] == line


def test_a_room_that_does_not_answer_within_3_minutes_goes_back_to_the_release_before(layout):
    probe = Probe(up=lambda n: n > UPDATE_TIMEOUT_S // HEALTH_INTERVAL_S)   # silent for the whole watch, then up again on A
    m = Machine(layout, probe=probe)
    m.job("update", commit=B)
    assert m.running() == f"releases/{A}/venv"
    assert layout.version_file.read_text() == A + "\n"
    assert m.runner.systemctl() == [["systemctl", "restart", "croom"], ["systemctl", "restart", "croom"]]   # the helper stays as it is
    assert probe.asked == UPDATE_TIMEOUT_S // HEALTH_INTERVAL_S + 1
    record = m.record()
    assert record["state"] == "rolled_back"
    assert record["why"] == "The room did not answer within 3 minutes of starting bbbbbbb"
    assert record["lines"][-2:] == ["The room did not answer within 3 minutes of starting bbbbbbb: going back to aaaaaaa", "Back on aaaaaaa"]
    assert releases(layout) == [A, P]                                # the release that did not start is gone; the two kept stay
    assert m.state()["previous_release"] == P


def test_a_room_that_does_not_answer_on_the_release_before_either_says_so(layout):
    m = Machine(layout, probe=Probe(up=False))
    m.job("update", commit=B)
    assert m.running() == f"releases/{A}/venv"
    assert m.record()["state"] == "rolled_back"
    assert m.record()["why"].endswith(", and the room has not answered on aaaaaaa either")


def test_the_watch_after_an_update_is_3_minutes(layout):
    probe = Probe(up=lambda n: n * HEALTH_INTERVAL_S >= UPDATE_TIMEOUT_S)    # answers at the last look of the 3 minutes
    m = Machine(layout, probe=probe)
    m.job("update", commit=B)
    assert UPDATE_TIMEOUT_S > HEALTH_TIMEOUT_S
    assert m.record()["state"] == "done" and m.running() == f"releases/{B}/venv"


# --- refusals


@pytest.mark.parametrize("args, words", [
    ({}, "must be a commit"),
    ({"commit": "main"}, "must be a commit"),
    ({"commit": B.upper()}, "must be a commit"),
    ({"commit": B, "url": "https://example.com/evil.git"}, "does not take url"),
    ({"commit": A}, "runs that version already"),
])
def test_update_refusals(layout, args, words):
    m = Machine(layout)
    with pytest.raises(JobError, match=words):
        m.jobs.table["update"](args)
    assert m.runner.calls == [] and m.record() == {"state": "none"}


def test_only_a_commit_on_main_is_installed(layout):
    m = Machine(layout, github=FakeGitHub(on_main=False))
    with pytest.raises(JobError, match="That version is not on main on GitHub"):
        m.job("update", commit=B)
    m = Machine(layout, github=FakeGitHub(on_main=GitHubError()))
    with pytest.raises(JobError, match=CANT_CHECK):
        m.job("update", commit=B)
    assert m.runner.calls == []


def test_a_tv_pi_without_the_release_layout_is_told_to_run_the_installer(paths):
    (paths.install_dir / "venv" / "bin").mkdir(parents=True)          # the venv the installer made before releases
    m = Machine(paths)
    with pytest.raises(JobError, match="installer"):
        m.job("update", commit=B)
    assert NEEDS_INSTALLER.startswith("This TV Pi can't update itself")
    assert m.job("version")["version"] == "unknown"


def test_while_an_update_runs_nothing_else_that_restarts_or_writes_may_begin(layout):
    m = Machine(layout, background=lambda work: None)                 # the update's work never ends here
    m.job("update", commit=B)
    for job, args in (("update", {"commit": B}), ("rollback", {}), ("restart", {}), ("reboot", {}), ("write_config", {"yaml": GOOD})):
        with pytest.raises(JobError, match=UPDATE_UNDER_WAY):
            m.jobs.table[job](args)


def test_an_update_is_refused_while_a_restart_is_watched(layout):
    m = Machine(layout, background=lambda work: None)
    m.job("restart")
    with pytest.raises(JobError, match="The room is restarting"):
        m.job("update", commit=B)


def test_a_refused_update_leaves_the_helper_free(layout):
    m = Machine(layout, github=FakeGitHub(on_main=False))
    with pytest.raises(JobError):
        m.job("update", commit=B)
    m.github.on_main = True
    assert m.job("update", commit=B) == {"updating": True}


# --- cut off (a power cut, a reboot, the helper stopped) and the sweep


def test_an_update_cut_off_while_building_is_closed_and_its_folder_removed_at_the_next_start(layout):
    release(layout, B, complete=False)                                 # pip was half way through
    state = read_state(layout)
    state["update"] = {"state": "building", "commit": B, "from": A, "lines": ["Installing bbbbbbb from GitHub: this takes several minutes"]}
    write_state(layout, state)
    m = Machine(layout)
    m.jobs.resume()
    assert m.running() == f"releases/{A}/venv" and releases(layout) == [A, P]
    assert m.record()["state"] == "failed"
    assert m.record()["lines"][-1] == "The update was cut off before it was finished: the room is still on aaaaaaa"
    assert m.runner.calls == []


def test_the_sweep_removes_only_unfinished_release_folders_and_stray_links(layout):
    release(layout, B, complete=False)
    release(layout, "d" * 40)                                           # whole, but neither running nor kept: left to the next update
    os.symlink("releases/x/venv", layout.install_dir / ".venv-x.123")
    m = Machine(layout)
    assert m.jobs._updater.sweep() == [B]
    assert releases(layout) == [A, P, "d" * 40]
    assert not (layout.install_dir / ".venv-x.123").exists()


def test_the_sweep_never_removes_the_running_release_even_without_its_version(paths):
    release(paths, A, complete=False)
    link(paths, A)
    Machine(paths).jobs.resume()
    assert releases(paths) == [A]


def write_record(paths, **record):
    state = read_state(paths)
    state["update"] = dict({"commit": B, "from": A, "lines": []}, **record)
    write_state(paths, state)


@pytest.mark.parametrize("state, restarts", [("watching", 0), ("restarting", 1), ("switching", 1)])
def test_an_update_cut_off_after_the_switch_is_watched_again_and_kept_when_the_room_answers(layout, state, restarts):
    release(layout, B)
    link(layout, B)
    write_record(layout, state=state)
    m = Machine(layout)
    m.jobs.resume()
    assert m.record()["state"] == "done" and m.running() == f"releases/{B}/venv"
    assert m.runner.systemctl().count(["systemctl", "restart", "croom"]) == restarts
    assert m.state()["previous_release"] == A


def test_an_update_cut_off_after_the_switch_goes_back_when_the_room_does_not_answer(layout):
    release(layout, B)
    link(layout, B)
    write_record(layout, state="watching")
    m = Machine(layout, probe=Probe(up=lambda n: n > UPDATE_TIMEOUT_S // HEALTH_INTERVAL_S))
    m.jobs.resume()
    assert m.running() == f"releases/{A}/venv" and m.record()["state"] == "rolled_back"
    assert releases(layout) == [A, P]


def test_an_update_cut_off_before_the_switch_happened_is_closed(layout):
    release(layout, B)
    write_record(layout, state="switching")                            # the record was written; the link was not yet moved
    m = Machine(layout)
    m.jobs.resume()
    assert m.running() == f"releases/{A}/venv" and m.record()["state"] == "failed"
    assert m.runner.calls == []


def test_a_going_back_cut_off_is_finished(layout):
    release(layout, B)
    link(layout, B)
    write_record(layout, state="rolling_back", why="The room did not answer within 3 minutes of starting bbbbbbb")
    m = Machine(layout)
    m.jobs.resume()
    assert m.running() == f"releases/{A}/venv" and m.record()["state"] == "rolled_back"
    assert m.runner.systemctl() == [["systemctl", "restart", "croom"]]


def test_resume_after_an_update_that_ended_does_nothing(layout):
    write_record(layout, state="done")
    m = Machine(layout)
    m.jobs.resume()
    assert m.runner.calls == [] and m.record()["state"] == "done"


# --- switch back, version, status


def test_switch_back_goes_to_the_release_kept_before_and_keeps_the_one_it_left(layout):
    m = Machine(layout)
    assert m.job("rollback") == {"rolling_back": True}
    assert m.running() == f"releases/{P}/venv" and m.record()["state"] == "done" and m.record()["rollback"] is True
    assert m.state()["previous_release"] == A and releases(layout) == [A, P]
    assert not [c for c in m.runner.calls if "pip" in " ".join(c)]


def test_switch_back_that_does_not_answer_returns_to_where_it_was(layout):
    m = Machine(layout, probe=Probe(up=lambda n: n > UPDATE_TIMEOUT_S // HEALTH_INTERVAL_S))
    m.job("rollback")
    assert m.running() == f"releases/{A}/venv" and m.record()["state"] == "rolled_back"
    assert releases(layout) == [A, P]                                # the one kept before is never removed


def test_switch_back_with_nothing_kept_is_refused(paths):
    release(paths, A)
    link(paths, A)
    with pytest.raises(JobError, match="no earlier version"):
        Machine(paths).job("rollback")


def test_version_says_the_running_release_when_it_was_installed_and_the_one_before(layout):
    version = Machine(layout).job("version")
    assert version["version"] == A and version["previous"] == P
    assert version["installed"][:4].isdigit() and "T" in version["installed"]


def test_update_status_before_any_update(paths):
    assert Machine(paths).record() == {"state": "none"}


# --- Task 8 fix round: nothing the helper acts on comes from a name it did not check


def test_a_forged_record_naming_a_path_is_set_aside_and_nothing_is_removed(layout, tmp_path):
    """The reviewer's case: a record whose commit is a path, and a room that does not answer. Nothing outside releases/ is touched,
    and the link stays where it is."""
    victim = tmp_path / "victim"
    (victim / "venv").mkdir(parents=True)
    (victim / "VERSION").write_text("x\n")
    write_record(layout, state="watching", commit="../../victim")
    m = Machine(layout, probe=Probe(up=False))
    m.jobs.resume()
    assert victim.exists() and (victim / "VERSION").exists()
    assert m.running() == f"releases/{A}/venv" and releases(layout) == [A, P]
    assert m.record()["state"] == "failed" and "commit" not in m.record() and m.runner.calls == []


@pytest.mark.parametrize("name", ["../../victim", "/etc", "a" * 39, "unknown/../..", "local-x"])
def test_a_name_that_is_not_a_releases_is_never_made_into_a_path(layout, name):
    m = Machine(layout)
    with pytest.raises(JobError):
        m.jobs._updater._release(name)
    m.jobs._updater._remove(name)
    assert releases(layout) == [A, P]


def test_a_forged_kept_release_does_not_let_the_kept_one_be_pruned_or_a_path_be_kept(layout):
    state = read_state(layout)
    state["previous_release"] = "../../opt"
    write_state(layout, state)
    m = Machine(layout)
    assert m.job("version")["previous"] is None
    with pytest.raises(JobError, match="no earlier version"):
        m.job("rollback")


def test_an_absent_release_before_never_becomes_the_link(layout):
    """A room on B that does not answer, whose record says it came from a release that is not on the TV Pi: the link is not pointed
    at nothing (the room software and the helper would both be gone); it stays, and the record says why."""
    release(layout, B)
    link(layout, B)
    absent = "d" * 40
    write_record(layout, state="watching", **{"from": absent})
    m = Machine(layout, probe=Probe(up=False))
    m.jobs.resume()
    assert m.running() == f"releases/{B}/venv"
    record = m.record()
    assert record["state"] == "failed" and "is not whole on this TV Pi" in record["lines"][-1]
    assert not (layout.releases / absent).exists()


@pytest.mark.parametrize("spoil", ["VERSION", "folder", "bin", "python"])
def test_a_release_that_is_not_roots_alone_is_neither_reused_nor_switched_to(layout, spoil):
    """A release whose folder, VERSION, venv/bin or venv/bin/python another user could change is not run as root: an update builds it
    afresh, and Switch back refuses it."""
    for name in (B, P):
        folder = layout.releases / name
        if not folder.exists():
            release(layout, name)
        target = {"VERSION": folder / "VERSION", "folder": folder, "bin": folder / "venv" / "bin",
                  "python": folder / "venv" / "bin" / "python"}[spoil]
        target.chmod(target.stat().st_mode | 0o022)
    m = Machine(layout)
    with pytest.raises(JobError, match="no earlier version"):
        m.job("rollback")
    m.job("update", commit=B)
    assert [c for c in m.runner.calls if c[0].endswith("/pip")]                       # built again, not reused
    assert m.record()["state"] == "done" and m.running() == f"releases/{B}/venv"


def test_the_switch_refuses_a_release_that_is_not_whole(layout):
    release(layout, B, complete=False)
    m = Machine(layout)
    with pytest.raises(JobError, match="not whole"):
        m.jobs._updater._switch(B)
    assert m.running() == f"releases/{A}/venv"


def test_a_new_release_is_sealed_and_on_the_disk_before_its_version_is_written(layout):
    m = Machine(layout)
    m.job("update", commit=B)
    lib = layout.releases / B / "venv" / "lib"
    assert stat.S_IMODE(lib.stat().st_mode) == 0o755 and stat.S_IMODE((lib / "croom.py").stat().st_mode) == 0o644
    assert m.syncs == [[A, P]]                                       # synced once, before B's VERSION existed


def test_an_update_waits_for_a_written_config_to_be_restarted_with(layout):
    m = Machine(layout)
    m.jobs._update_state(last_write=m.clock() - 60)                   # written, and no restart since
    with pytest.raises(JobError, match="Restart now first, then update"):
        m.job("update", commit=B)
    with pytest.raises(JobError, match="Restart now first, then update"):
        m.job("rollback")
    m.runner.answers["systemctl show croom"] = (0, f"@{m.clock() - 10:.0f}\n")   # the room software started after it (a reboot)
    assert m.job("update", commit=B) == {"updating": True}


def test_a_restart_asked_for_after_the_write_lets_the_update_go(layout):
    m = Machine(layout)
    m.jobs._update_state(last_write=m.clock() - 60, watch_asked_at=m.clock() - 30)
    assert m.job("update", commit=B) == {"updating": True}


def test_after_a_boot_the_watch_waits_for_the_room_software_to_start(layout):
    """Taken up at the helper's start (a boot): the 3 minutes count from when systemd says the room software is active, so a slow
    desktop does not undo a good update."""
    release(layout, B)
    link(layout, B)
    write_record(layout, state="watching")
    started_at = {}
    probe = Probe(up=lambda n: n * HEALTH_INTERVAL_S > 60)             # answers a minute into the watch
    m = Machine(layout, probe=probe)
    began = m.clock()
    m.runner.answers["systemctl is-active"] = lambda: (0, "") if m.clock() - began >= 200 else (3, "")
    real = probe.__call__

    def first(host, port):
        started_at.setdefault("at", m.clock())
        return real(host, port)
    m.jobs._host.probe = first
    m.jobs.resume()
    assert started_at["at"] - began >= 200                             # no look before the room software was active
    assert m.record()["state"] == "done" and m.running() == f"releases/{B}/venv"


def test_the_wait_for_the_room_software_is_capped_at_5_minutes(layout):
    release(layout, B)
    link(layout, B)
    write_record(layout, state="watching")
    m = Machine(layout, probe=Probe(up=False))
    began = m.clock()
    m.runner.answers["systemctl is-active"] = (3, "")
    m.jobs.resume()
    waited = len([c for c in m.runner.calls if c[:2] == ["systemctl", "is-active"]])
    assert waited * HEALTH_INTERVAL_S <= 300 + HEALTH_INTERVAL_S
    assert m.clock() - began >= 300 + UPDATE_TIMEOUT_S
    assert m.record()["state"] == "rolled_back"


@pytest.mark.parametrize("state", ["restarting", "watching"])
def test_an_update_whose_link_is_not_on_the_new_release_is_closed_without_a_watch_or_a_prune(layout, state):
    release(layout, B)
    release(layout, "d" * 40)                                          # a third: not pruned, nothing is credited
    write_record(layout, state=state)                                   # the link is still on A
    m = Machine(layout)
    m.jobs.resume()
    assert m.record()["state"] == "failed"
    assert m.record()["lines"][-1] == "The update was cut off before it was finished: the room is still on aaaaaaa"
    assert m.probe.asked == 0 and m.runner.calls == []
    assert releases(layout) == [A, B, P, "d" * 40] and m.state()["previous_release"] == P


def test_an_orphaned_third_release_is_pruned_at_start(layout):
    release(layout, "d" * 40)
    m = Machine(layout)
    m.jobs.resume()
    assert releases(layout) == [A, P]


# --- the final review: Switch back only to a release whose helper starts; an update waits for a meeting to end


def helper_check(paths, name):
    return f"{paths.releases / name / 'venv' / 'bin' / 'python'} -m croom.admin --check"


def test_a_kept_release_whose_helper_does_not_start_is_not_offered_and_switch_back_to_it_is_refused(layout):
    """A room upgraded by the installer keeps the release from before room settings: it has no croom.admin, so switched back to, the
    helper would not start again (systemd would start it over and over). Software does not offer it, and rollback refuses it."""
    m = Machine(layout)
    m.runner.answers[helper_check(layout, P)] = (1, "/opt/croom/releases/c/venv/bin/python: No module named croom.admin")
    assert m.job("version")["previous"] is None
    with pytest.raises(JobError, match="can't be switched back to: it has no Settings helper that starts"):
        m.job("rollback")
    assert m.running() == f"releases/{A}/venv" and m.runner.systemctl() == []
    assert releases(layout) == [A, P]                                 # still kept: only the offer is gone


def test_the_kept_releases_helper_is_checked_once_and_a_good_one_is_offered(layout):
    m = Machine(layout)
    for _ in range(3):
        assert m.job("version")["previous"] == P
    assert [c for c in m.runner.calls if " ".join(c) == helper_check(layout, P)] == [[str(layout.releases / P / "venv" / "bin" / "python"),
                                                                                      "-m", "croom.admin", "--check"]]
    assert m.job("rollback") == {"rolling_back": True} and m.running() == f"releases/{P}/venv"


def test_going_back_to_a_release_whose_helper_does_not_start_still_brings_the_room_back_and_leaves_the_helper_as_it_is(layout):
    """An update taken up at the helper's start (the helper is the new release's), the room silent on it, and the release before it has a
    helper that does not start: the agent goes back all the same (the room must come up), the helper is not restarted on it, and the
    record says the helper stays on the new release until it is mended."""
    release(layout, B)
    link(layout, B)
    write_record(layout, state="watching")
    m = Machine(layout, probe=Probe(up=lambda n: n > UPDATE_TIMEOUT_S // HEALTH_INTERVAL_S))
    m.runner.answers[helper_check(layout, A)] = (1, "No module named croom.admin")
    m.jobs.resume()
    assert m.running() == f"releases/{A}/venv"
    assert m.runner.systemctl() == [["systemctl", "is-active", "--quiet", "croom"], ["systemctl", "restart", "croom"]]
    record = m.record()
    assert record["state"] == "rolled_back" and record["helper_stays"] == B
    assert "The helper of aaaaaaa does not start, so it is not restarted on it: the helper stays on bbbbbbb until this is mended " \
           "(run the installer again)" in record["lines"]


def test_an_update_waits_for_a_meeting_on_the_room_to_end_before_it_switches(layout):
    m = Machine(layout)
    began = m.clock()
    switched_at = {}
    m.busy = lambda now: now - began < 10 * 60                        # a meeting that ends ten minutes in
    m.runner.on_call = lambda args: switched_at.setdefault("at", m.clock() - began) if args == ["systemctl", "restart", "croom"] else None
    m.job("update", commit=B)
    assert switched_at["at"] >= 10 * 60 and m.record()["state"] == "done"
    lines = m.record()["lines"]
    assert lines[lines.index("Checking that its helper starts") + 1:lines.index("Switched to bbbbbbb")] == [
        "A meeting is on: waiting for it to end before the room restarts (up to 30 minutes)", "The meeting has ended"]
    assert {path for _, _, path in m.asked} == {"/api/devices/busy"} and {host for host, _, _ in m.asked} == {"127.0.0.1"}


def test_an_update_waits_30_minutes_at_most_for_a_meeting_and_says_so(layout):
    m = Machine(layout)
    began = m.clock()
    m.busy = True
    m.job("rollback")
    assert 30 * 60 <= m.clock() - began < 31 * 60 + UPDATE_TIMEOUT_S
    assert m.running() == f"releases/{P}/venv" and m.record()["state"] == "done"
    assert "The meeting has gone on for 30 minutes: the room restarts all the same, which ends it" in m.record()["lines"]


def test_while_the_update_waits_for_the_meeting_its_record_is_under_way(layout):
    """The room refuses a join while the update's record says it is under way: no meeting starts in the wait."""
    m = Machine(layout)
    states = []
    m.busy = lambda now: states.append(m.record()["state"]) or len(states) < 3
    m.job("update", commit=B)
    assert states and set(states) <= {"building"}


def test_a_save_of_live_settings_only_does_not_hold_an_update_up(layout):
    """The booking hours apply at once: a save of only them leaves nothing waiting for a restart, so Update goes ahead. A save of a
    setting that waits for a restart is still told to restart first."""
    m = Machine(layout)
    m.job("write_config", yaml=GOOD + "calendar:\n  booking_start: '08:00'\n  booking_end: '18:00'\n")
    assert m.job("update", commit=B) == {"updating": True}
    m.job("write_config", yaml="room:\n  name: Conference B\ncalendar:\n  booking_start: '08:00'\n  booking_end: '18:00'\n")
    with pytest.raises(JobError, match="Restart now first, then update"):
        m.job("rollback")
