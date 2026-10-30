"""The touch Pi's agent (room settings spec 2026-10-29, sections 6 and 7): its identity, the check-in with the room (the real registry
behind a fake HTTP), its jobs (reboot, update; the Wi-Fi ones are in test_wifi.py), the update's build, switch, kiosk setup and reboot, the
5 minutes the new version has to check in after it (the good agent supervising a fake child), going back, and what each start does after
a power cut at any point. Everything outside the agent's folders is a stand-in (fakes.py)."""

import json
import os
import stat

import pytest

from tests.unit.kiosk_agent.fakes import URL, USER, A, B, C, Device, PowerCut, agent, release

TOKEN_HEADER = "X-Croom-Device-Token"


@pytest.fixture
def device(tmp_path):
    return Device(tmp_path)


def allowed(device, version=None):
    """The touch Pi has asked to join and was let in."""
    me = device.agent(version)
    me.start()
    me.checkin()
    device.room.registry.allow(me.identity["id"])
    return me


def give(device, me, job, **args):
    """The room gives the touch Pi a job, and the agent takes it at its next check-in and does it."""
    queued = device.room.registry.queue(me.identity["id"], job, args)
    me.checkin()
    me.wait(5)
    return queued


def last(device, me):
    return device.room.registry.get(me.identity["id"])["last"]


# --- identity and check-in

def test_the_identity_is_made_once_and_kept_for_root_alone(device):
    me = device.agent()
    me.start()
    kept = json.loads(device.paths.identity.read_text())
    assert kept == me.identity and len(kept["id"]) == 32 and len(kept["token"]) == 43
    assert stat.S_IMODE(os.stat(device.paths.identity).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(device.paths.state).st_mode) == 0o700
    again = device.agent()
    again.start()
    assert again.identity == kept


def test_it_waits_to_join_then_checks_in_with_its_token(device):
    me = device.agent()
    me.start()
    assert me.checkin() == {"state": "pending", "interval": 5}
    url, body, headers = device.room.posts[-1]
    assert url == "http://pimeet-3.local:8080/api/devices/checkin"
    assert headers[TOKEN_HEADER] == me.identity["token"]
    assert body == {"id": me.identity["id"], "kind": "touch", "version": A, "name": "touch-pi",
                    "network": {"address": "10.0.0.9", "wifi": True, "connection": "wifi", "ssid": "Office", "signal": 70,
                                "saved": ["Office"], "change": None},
                    "job": None}
    [waiting] = device.room.registry.waiting()
    assert waiting["name"] == "touch-pi" and waiting["version"] == A
    device.room.registry.allow(me.identity["id"])
    assert me.checkin() == {"state": "allowed", "job": None, "interval": 5}


def test_a_room_that_does_not_answer_or_refuses_is_ridden_out(device, caplog):
    me = allowed(device)
    device.room.down = True
    assert me.checkin() is None and me.checkin() is None
    assert sum("Can't reach the room" in r.message for r in caplog.records) == 1     # said once, not every 5 seconds
    device.room.down = False
    device.room.registry.refuse(me.identity["id"])
    assert me.checkin() is None


# --- jobs

def test_reboot_reboots_and_says_so_after_it(device):
    me = allowed(device)
    job = give(device, me, "reboot")
    assert device.runner.calls[-1] == ["systemctl", "reboot"] and device.syncs
    assert device.job()["state"] == "running" and device.job()["words"] == "Rebooting"
    me.checkin()
    assert device.room.registry.get(me.identity["id"])["job"]["state"] == "running"
    after = device.agent()           # the touch Pi has rebooted
    after.start()
    after.checkin()
    assert last(device, after)["id"] == job["id"] and last(device, after)["state"] == "done" and last(device, after)["words"] == "Rebooted"


def test_a_reboot_that_fails_says_why(device):
    device.runner.reboot_fails = True
    me = allowed(device)
    give(device, me, "reboot")
    me.checkin()
    assert last(device, me)["state"] == "failed" and last(device, me)["words"] == "Could not reboot: Failed to reboot"


def test_a_job_is_done_once_and_one_at_a_time(device):
    me = allowed(device)
    assert me.take({"id": "1" * 16, "type": "wifi_scan", "args": {}})
    me.wait(5)
    assert not me.take({"id": "1" * 16, "type": "reboot", "args": {}})        # the same id: had already
    assert device.runner.calls == []
    for bad in ({"id": "../x", "type": "reboot"}, {"id": "2" * 16, "type": "rm"}, {"id": 3, "type": "reboot"}):
        assert not me.take(bad)
    assert device.runner.calls == []


def test_a_job_handed_out_while_another_runs_waits(device, monkeypatch):
    import threading
    gate = threading.Event()
    me = allowed(device)
    monkeypatch.setitem(agent.JOBS, "wifi_scan", lambda self, job_id, args: gate.wait(5) and "scanned")
    assert me.take({"id": "1" * 16, "type": "wifi_scan", "args": {}})
    assert not me.take({"id": "2" * 16, "type": "reboot", "args": {}})
    gate.set()
    me.wait(5)
    assert me.take({"id": "2" * 16, "type": "reboot", "args": {}})
    me.wait(5)


def test_the_trial_agent_says_it_checked_in_and_does_no_job(device):
    me = allowed(device)
    device.room.registry.queue(me.identity["id"], "reboot", {})
    trial = device.agent(version=B, trial=True)
    assert trial.start() is None
    assert not device.paths.trial_ok.exists()
    trial.checkin()
    assert device.paths.trial_ok.read_text() == B
    assert device.runner.calls == [] and device.job() == {}
    assert device.room.posts[-1][1]["version"] == B


def test_a_trial_agent_that_is_not_let_in_says_nothing(device):
    trial = device.agent(version=B, trial=True)
    trial.start()
    trial.checkin()
    assert not device.paths.trial_ok.exists()


# --- update: building and switching

def git(device):
    return [call for call in device.runner.calls if call[0] == "git"]


def test_an_update_builds_beside_the_running_one_sets_it_up_and_reboots(device):
    me = allowed(device)
    job = give(device, me, "update", commit=B)
    repo = str(device.paths.repo)
    assert git(device)[0] == ["git", "-C", repo, "fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main"]
    assert git(device)[1] == ["git", "-C", repo, "merge-base", "--is-ancestor", B, "refs/remotes/origin/main"]
    assert git(device)[2][:5] == ["git", "-C", repo, "archive", "--format=tar"] and git(device)[2][-1] == B
    tar = next(c for c in device.runner.calls if c[0] == "tar")
    assert "--no-same-owner" in tar and "--no-same-permissions" in tar
    new = device.paths.releases / B
    assert (new / "VERSION").read_text() == B + "\n"
    assert (new / "tools" / "kiosk_agent" / "croom_kiosk_agent.py").read_text() == f"# agent of {B}\n"
    for path in new.rglob("*"):
        assert not os.lstat(path).st_mode & (stat.S_IWGRP | stat.S_IWOTH), path      # sealed: git's group-writable files are not
    assert not list(device.paths.releases.glob(".partial-*")) and not list(device.paths.base.glob(".*.tar"))
    # The kiosk setup of the new release, as the installer runs it, for the desktop user, in its agent mode
    setup = next(c for c in device.runner.calls if c[0] == "bash")
    assert setup == ["bash", str(new / "installer" / "install-kiosk.sh"), "--url", URL]
    env = device.runner.envs[-1]
    assert env["KIOSK_USER"] == USER and env["KIOSK_FROM_AGENT"] == "1" and "sudo" not in json.dumps(env)
    assert device.runner.calls[-1] == ["systemctl", "reboot"]
    # The kiosk is on the new release; the agent systemd starts is still the old one, which proves the new one after the reboot
    assert device.links() == (B, A)
    record = device.record()
    assert record["state"] == "trial" and record["commit"] == B and record["from"] == A and record["job"] == job["id"]
    assert device.job()["state"] == "running" and device.job()["words"] == "Restarting on bbbbbbb"
    assert stat.S_IMODE(os.stat(device.paths.update).st_mode) == 0o600


def test_a_release_on_the_touch_pi_already_is_used_again(device):
    me = allowed(device)
    release(device.paths, B)     # (after the start: its sweep removes a release that is not kept)
    give(device, me, "update", commit=B)
    assert not [c for c in git(device) if "archive" in c]
    assert device.links() == (B, A)


def test_a_release_that_others_could_have_written_is_not_used(device):
    me = allowed(device)
    release(device.paths, B, mode=0o666)     # (after the start: its sweep removes a release that is not kept)
    give(device, me, "update", commit=B)
    assert [c for c in git(device) if "archive" in c]
    assert not os.lstat(device.paths.releases / B / "VERSION").st_mode & stat.S_IWOTH


@pytest.mark.parametrize("commit, prepare, words", [
    ("main", None, "Send the version to install (a commit)"),
    (A, None, "This touch Pi runs that version already"),
    ("d" * 40, None, "The update to ddddddd did not happen: That version is not on main on GitHub. The touch Pi is still on aaaaaaa"),
    (B, "offline", "The update to bbbbbbb did not happen: Can't reach GitHub (fatal: unable to access 'https://github.com/': Could not "
                   "resolve host). The touch Pi is still on aaaaaaa"),
])
def test_an_update_that_cannot_be_done_leaves_the_running_release(device, commit, prepare, words):
    if prepare == "offline":
        device.runner.offline = True
    me = allowed(device)
    device.room.registry.queue(me.identity["id"], "update", {"commit": B})   # (the TV Pi checks the format too: hand it out as given)
    me.take({"id": "1" * 16, "type": "update", "args": {"commit": commit}})
    me.wait(5)
    assert device.job()["state"] == "failed" and device.job()["words"] == words
    assert device.links() == (A, A) and sorted(p.name for p in device.paths.releases.iterdir()) == [A]
    assert ["systemctl", "reboot"] not in device.runner.calls and device.runner.setups == []


def test_a_touch_pi_not_set_up_for_updates_says_so(device):
    me = allowed(device, version="unknown")
    me.take({"id": "1" * 16, "type": "update", "args": {"commit": B}})
    me.wait(5)
    assert device.job()["words"] == "This touch Pi was not set up for updates: run install-kiosk.sh on it once more"


def test_a_kiosk_setup_that_fails_goes_back_without_a_reboot(device):
    device.runner.bad_setup = {B}
    me = allowed(device)
    give(device, me, "update", commit=B)
    assert device.runner.setups == [B, A]
    assert device.links() == (A, A) and device.runner.reboots == 0
    assert device.record()["state"] == "failed"
    me.checkin()
    assert last(device, me)["words"] == ("The update to bbbbbbb did not happen: its kiosk setup did not finish (E: no space left). "
                                         "The touch Pi is still on aaaaaaa")


# --- after the reboot: the 5 minutes

def updated(device):
    """The touch Pi has been told to update to B and has rebooted: the agent systemd starts (the good one, A) is returned, started."""
    me = allowed(device)
    give(device, me, "update", commit=B)
    assert device.runner.reboots == 1
    after = device.agent()
    assert after.version == A
    return after


def checks_in_after(polls):
    def behaviour(child):
        if child.polls >= polls:
            child.args_path = child.args[1]
            (child_state(child) / "trial-ok").write_text(B)
    return behaviour


def child_state(child):
    from pathlib import Path
    return Path(child.args[child.args.index("--state") + 1])


def test_the_new_version_checks_in_and_becomes_the_good_one(device):
    after = updated(device)
    device.child_does = checks_in_after(3)
    assert after.start() == "trial"
    assert after.supervise() == "promoted"
    [child] = device.children
    assert child.args == ["/usr/bin/python3", str(device.paths.releases / B / "tools" / "kiosk_agent" / "croom_kiosk_agent.py"),
                          "--trial", "--base", str(device.paths.base), "--state", str(device.paths.state)]
    assert child.terminated
    assert device.links() == (B, B)
    assert device.record()["state"] == "done"
    assert device.job()["state"] == "done" and device.job()["words"] == "Updated to bbbbbbb"
    assert not device.paths.trial_ok.exists()
    # The agent starts over from the new good release
    assert device.execs == [["/usr/bin/python3", str(device.paths.good / "tools" / "kiosk_agent" / "croom_kiosk_agent.py"),
                             "--base", str(device.paths.base), "--state", str(device.paths.state)]]
    assert device.runner.reboots == 1
    new = device.agent()
    assert new.version == B and new.start() is None
    new.checkin()
    assert last(device, new)["state"] == "done" and last(device, new)["words"] == "Updated to bbbbbbb"
    assert device.room.registry.get(new.identity["id"])["version"] == B
    assert sorted(p.name for p in device.paths.releases.iterdir()) == [A, B]      # the one before is kept


def test_a_new_version_that_does_not_check_in_within_5_minutes_is_undone(device):
    after = updated(device)
    assert after.start() == "trial"
    started = device.clock()
    assert after.supervise() == "rebooting"
    assert 300 <= device.clock() - started < 302
    assert device.children[-1].terminated
    assert device.links() == (A, A)
    assert device.runner.setups == [B, A] and device.runner.reboots == 2
    assert device.record()["state"] == "rolled_back"
    words = "The update to bbbbbbb was undone: the touch Pi did not check in with it within 5 minutes. It is back on aaaaaaa"
    assert device.job()["state"] == "failed" and device.job()["words"] == words
    back = device.agent()           # after the reboot onto A
    assert back.start() is None and device.runner.reboots == 2
    back.checkin()
    assert last(device, back)["state"] == "failed" and last(device, back)["words"] == words


def test_a_new_agent_that_will_not_start_is_started_again_then_undone(device):
    def crashes(child):
        child.code = 1
    device.child_does = crashes
    after = updated(device)
    after.start()
    assert after.supervise() == "rebooting"
    assert 50 <= len(device.children) <= 61             # once every 5 seconds for 5 minutes
    assert device.links() == (A, A)


def test_the_new_version_must_check_in_as_itself(device):
    def wrong(child):
        (child_state(child) / "trial-ok").write_text(C)
    device.child_does = wrong
    after = updated(device)
    after.start()
    assert after.supervise() == "rebooting" and device.links() == (A, A)


def test_starts_cut_off_in_the_5_minutes_count_and_end_in_going_back(device):
    after = updated(device)
    for start in range(1, agent.MAX_TRIALS + 1):
        assert after.start() == "trial"
        device.clock.now += 1
        cut = {"n": 0}

        def power_cut(child):
            cut["n"] += 1
            if cut["n"] == 10:
                raise PowerCut()
        device.child_does = power_cut
        with pytest.raises(PowerCut):
            after.supervise()
        assert device.record()["trials"] == start
        after = device.agent()
    assert after.start() == "trial"
    assert after.supervise() == "rebooting"
    assert device.links() == (A, A)
    assert "after 3 starts" in device.job()["words"]


# --- a power cut at any point

def counting_cut(device, monkeypatch, at):
    """Cut the power at the `at`th step of the update (a command, a sync, a file written whole, a link moved)."""
    steps = {"n": 0}

    def step():
        steps["n"] += 1
        if steps["n"] == at:
            raise PowerCut()

    def wrap(real):
        def cut(*args, **kwargs):
            step()
            return real(*args, **kwargs)
        return cut
    device.runner.cut = step
    sync = device.sync
    device.sync = lambda: (step(), sync())
    monkeypatch.setattr(agent, "write_json", wrap(agent.write_json))
    monkeypatch.setattr(agent, "point", wrap(agent.point))
    return steps


def boot_until_settled(device, monkeypatch, proves):
    """Start the touch Pi again (no more cuts) until the agent checks in normally: a trial is proven or not, a going back reboots."""
    monkeypatch.undo()
    device.runner.cut = None
    device.sync = lambda: None
    device.child_does = checks_in_after(2) if proves else (lambda child: None)
    for _ in range(4):
        me = device.agent()
        plan = me.start()
        if plan == "trial":
            plan = me.supervise()
            if plan == "promoted":
                continue
        if plan is None:
            return me
    raise AssertionError("the touch Pi never settled")


@pytest.mark.parametrize("proves", [True, False])
def test_a_power_cut_at_any_point_of_an_update_leaves_one_whole_version(tmp_path, monkeypatch, proves):
    """The update, the reboot and the 5 minutes after it, with the power cut at its first step, then at its second, and so on until
    one runs to the end without reaching the cut: after each, the touch Pi is started again until it settles, and is then on one whole
    version, the old or (only when it checked in) the new, with its links agreeing and the job's result told."""
    import threading
    at = 0
    while True:
        at += 1
        device = Device(tmp_path / f"cut-{at}")
        me = allowed(device)
        device.child_does = checks_in_after(2) if proves else (lambda child: None)
        monkeypatch.setattr(threading, "excepthook", lambda hook: None if hook.exc_type is PowerCut else print(hook))
        steps = counting_cut(device, monkeypatch, at)
        queued = device.room.registry.queue(me.identity["id"], "update", {"commit": B})
        try:
            me.checkin()
            me.wait(5)
            if steps["n"] < at:             # the update ran to its reboot: the touch Pi starts again on the agent of before
                after = device.agent()
                if after.start() == "trial":
                    after.supervise()
        except PowerCut:
            pass
        if steps["n"] < at:
            break                           # nothing left to cut
        settled = boot_until_settled(device, monkeypatch, proves)
        current, good = device.links()
        assert current == good and current in (A, B), (at, device.record())
        assert settled.version == good and settled.trusted(good)
        assert device.record().get("state") in (None, "done", "failed", "rolled_back"), (at, device.record())
        assert not list(device.paths.releases.glob(".partial-*")) and not list(device.paths.base.glob(".*.tar"))
        if device.job().get("id") == queued["id"]:
            assert device.job()["state"] in ("done", "failed"), (at, device.job())
            assert (device.job()["state"] == "done") == (good == B), at
        if not proves:
            assert good == A, at
    assert at > 15, at        # the update has that many steps, and each was cut


def test_a_record_cut_off_while_building_is_closed_and_its_folder_removed(device):
    me = allowed(device)
    me._set_job(id="1" * 16, type="update", state="running", words="Getting bbbbbbb")
    (device.paths.releases / f".partial-{B}").mkdir()
    (device.paths.base / f".{B}.tar").write_text("half")
    agent.write_json(device.paths.update, {"state": "building", "commit": B, "from": A, "job": "1" * 16})
    after = device.agent()
    assert after.start() is None and device.runner.reboots == 0
    assert not (device.paths.releases / f".partial-{B}").exists() and not (device.paths.base / f".{B}.tar").exists()
    assert device.links() == (A, A)
    assert device.job()["state"] == "failed"
    assert device.job()["words"] == ("The update to bbbbbbb did not happen: it was cut off before it was finished. "
                                     "The touch Pi is still on aaaaaaa")


def test_a_record_cut_off_while_switching_goes_back(device):
    release(device.paths, B)
    agent.point(device.paths.current, B)
    agent.write_json(device.paths.update, {"state": "switching", "commit": B, "from": A, "job": "1" * 16})
    after = device.agent()
    assert after.start() == "rebooting"
    assert device.links() == (A, A) and device.runner.setups == [A] and device.record()["state"] == "rolled_back"


def test_a_promotion_cut_off_is_finished(device):
    release(device.paths, B)
    agent.point(device.paths.current, B)
    agent.point(device.paths.good, B)
    agent.write_json(device.paths.job, {"id": "1" * 16, "type": "update", "state": "running"})
    agent.write_json(device.paths.update, {"state": "trial", "commit": B, "from": A, "job": "1" * 16})
    after = device.agent()
    assert after.version == B and after.start() is None
    assert device.record()["state"] == "done" and device.job()["state"] == "done" and device.runner.reboots == 0


def test_an_ended_update_whose_job_was_not_told_is_told(device):
    agent.write_json(device.paths.job, {"id": "1" * 16, "type": "update", "state": "running"})
    agent.write_json(device.paths.update, {"state": "rolled_back", "commit": B, "from": A, "job": "1" * 16, "why": "it broke"})
    device.agent().start()
    assert device.job()["state"] == "failed" and device.job()["words"].startswith("The update to bbbbbbb was undone: it broke")


def test_a_job_the_agent_stopped_in_did_not_finish(device):
    agent.write_json(device.paths.job, {"id": "1" * 16, "type": "wifi_scan", "state": "running"})
    device.agent().start()
    assert device.job()["state"] == "failed" and device.job()["words"] == "The touch Pi restarted before it finished"


@pytest.mark.parametrize("names", [{"commit": "../../etc", "from": A}, {"commit": B, "from": "/"}, {"commit": B}])
def test_a_record_whose_names_are_not_commits_touches_nothing(device, names):
    victim = device.paths.base / "etc"
    victim.mkdir()
    agent.write_json(device.paths.update, dict({"state": "rolling_back", "job": "1" * 16}, **names))
    after = device.agent()
    assert after.start() is None
    assert victim.exists() and device.links() == (A, A) and device.runner.calls == []
    assert device.record()["state"] == "failed"


def test_the_sweep_keeps_the_good_the_current_and_the_one_before(device):
    for name in (B, C, "d" * 40):
        release(device.paths, name)
    agent.point(device.paths.good, B)
    agent.point(device.paths.current, B)
    agent.write_json(device.paths.update, {"state": "done", "commit": B, "from": C})
    (device.paths.releases / ".partial-x").mkdir()
    (device.paths.base / ".current-99").symlink_to("releases/" + A)
    device.agent().start()
    assert sorted(p.name for p in device.paths.releases.iterdir()) == [B, C]
    assert not (device.paths.base / ".current-99").is_symlink()


def test_going_back_to_a_release_that_is_not_whole_stays_and_says_so(device):
    release(device.paths, B)
    agent.point(device.paths.current, B)
    (device.paths.releases / A / "VERSION").unlink()
    agent.write_json(device.paths.update, {"state": "rolling_back", "commit": B, "from": A, "job": "1" * 16, "why": "it broke"})
    device.agent(version=A).start()
    assert device.links() == (B, A) and device.runner.setups == []


# --- the real git and tar

def test_with_the_real_git_and_tar(tmp_path):
    """The commands as they are run, on a clone of a repository with a main of two commits and a commit that is not on main."""
    import shutil
    import subprocess
    if shutil.which("git") is None or shutil.which("tar") is None:
        pytest.skip("git or tar not here")

    def sh(*args, cwd=None):
        return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    origin = tmp_path / "origin"
    origin.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=origin)
    sh("git", "config", "user.email", "t@example.com", cwd=origin)
    sh("git", "config", "user.name", "T", cwd=origin)
    (origin / "installer").mkdir()
    (origin / "tools" / "kiosk_agent").mkdir(parents=True)
    (origin / "installer" / "install-kiosk.sh").write_text("# one\n")
    (origin / "tools" / "kiosk_agent" / "croom_kiosk_agent.py").write_text("# one\n")
    sh("git", "add", ".", cwd=origin)
    sh("git", "commit", "-q", "-m", "one", cwd=origin)
    first = sh("git", "rev-parse", "HEAD", cwd=origin)
    device = Device(tmp_path / "pi", running=first)
    sh("git", "clone", "-q", "--bare", str(origin), str(device.paths.repo))
    (origin / "installer" / "install-kiosk.sh").write_text("# two\n")
    sh("git", "commit", "-q", "-am", "two", cwd=origin)
    second = sh("git", "rev-parse", "HEAD", cwd=origin)
    sh("git", "checkout", "-q", "-b", "elsewhere", cwd=origin)
    (origin / "README").write_text("not on main\n")
    sh("git", "add", ".", cwd=origin)
    sh("git", "commit", "-q", "-m", "elsewhere", cwd=origin)
    stray = sh("git", "rev-parse", "HEAD", cwd=origin)
    sh("git", "checkout", "-q", "main", cwd=origin)

    played = device.runner

    def runner(args, env=None, timeout=None):
        if args[0] in ("bash", "systemctl"):
            return played(args, env=env, timeout=timeout)
        played.calls.append(list(args))
        return agent.run_command(args, env=env, timeout=timeout)
    device.runner = runner
    me = allowed(device)
    me.take({"id": "1" * 16, "type": "update", "args": {"commit": stray}})
    me.wait(30)
    assert "not on main" in device.job()["words"] and device.links() == (first, first)
    me.take({"id": "2" * 16, "type": "update", "args": {"commit": second}})
    me.wait(30)
    assert device.job()["words"] == f"Restarting on {second[:7]}", device.job()
    assert (device.paths.releases / second / "installer" / "install-kiosk.sh").read_text() == "# two\n"
    assert device.links() == (second, first) and played.setups == [second] and played.reboots == 1
