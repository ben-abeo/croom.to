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
    (paths.data_dir / "admin-state.json").write_text(json.dumps({"previous_release": P}))
    return paths


class Machine:
    def __init__(self, paths, probe=None, github=None, background=None):
        self.paths = paths
        self.runner, self.clock = Runner(), Clock()
        self.probe = probe or Probe()
        self.github = github or FakeGitHub()
        self.queued = []
        self.jobs = Jobs(Host(paths=paths, run=self.runner, run_long=self.runner, owner=getpass.getuser(),
                              group=grp.getgrgid(os.getgid()).gr_name, probe=self.probe, clock=self.clock, sleep=self.clock.sleep,
                              monotonic=self.clock, background=background or (lambda work: work()), github=self.github,
                              python="/usr/bin/python3"))

    def job(self, name, **args):
        return self.jobs.table[name](args)

    def record(self):
        return self.job("update_status")

    def running(self):
        return os.readlink(self.paths.venv_link)

    def state(self):
        return json.loads((self.paths.data_dir / "admin-state.json").read_text())


def releases(paths):
    return sorted(p.name for p in paths.releases.iterdir())


# --- an update that works


def test_an_update_builds_beside_the_running_release_switches_restarts_and_watches(layout):
    m = Machine(layout)
    assert m.job("update", commit=B) == {"updating": True}
    assert m.github.asked == [B]                                     # only a commit of main is installed
    venv = str(layout.releases / B / "venv")
    assert m.runner.calls[:4] == [
        ["/usr/bin/python3", "-m", "venv", venv],
        [venv + "/bin/pip", "install", "--no-input", "--disable-pip-version-check", f"git+{REPO_URL}@{B}"],
        ["runuser", "-u", getpass.getuser(), "--", "env", f"PLAYWRIGHT_BROWSERS_PATH={layout.install_dir / 'browsers'}",
         venv + "/bin/playwright", "install", "chromium"],
        [venv + "/bin/python", "-c", "import croom.core.agent, croom.admin.jobs"],
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
                           "Installing the meeting browser it uses", "Checking that it starts", "Switched to bbbbbbb",
                           "Restarting the room software"]) in seen


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
    state = json.loads((layout.data_dir / "admin-state.json").read_text())
    state["update"] = {"state": "building", "commit": B, "from": A, "lines": ["Installing bbbbbbb from GitHub: this takes several minutes"]}
    (layout.data_dir / "admin-state.json").write_text(json.dumps(state))
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
    state = json.loads((paths.data_dir / "admin-state.json").read_text())
    state["update"] = dict({"commit": B, "from": A, "lines": []}, **record)
    (paths.data_dir / "admin-state.json").write_text(json.dumps(state))


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
