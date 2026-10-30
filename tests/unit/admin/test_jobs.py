"""The helper's jobs (spec 2026-10-29, sections 4 and 5) against a fake machine: a temporary folder stands in for /etc/croom, /var/lib/croom,
/media and /opt/croom, a fake runner for systemctl, a fake probe for GET /api/status, and a fake clock whose sleep moves it."""

import getpass
import grp
import json
import os
import stat

import pytest
import yaml

from croom.admin.jobs import (
    HEALTH_TIMEOUT_S,
    MAX_USB_FILE,
    Host,
    JobError,
    Jobs,
    Paths,
    check_config_yaml,
)

GOOD = "room:\n  name: Conference A\ncontrol:\n  port: 8099\n"
GOOGLE_KEY = {"type": "service_account", "project_id": "croom", "client_email": "room@croom.iam.gserviceaccount.com",
              "private_key": "-----BEGIN PRIVATE KEY-----\nMII...\n-----END PRIVATE KEY-----\n"}
ZOOM = {"sdk_client_id": "abc", "sdk_client_secret": "def"}


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.codes = {}

    def __call__(self, args):
        self.calls.append(list(args))
        return self.codes.get(" ".join(args), 0), ""


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Probe:
    """GET /api/status: answers once `up` is true; `up` may be a function of the calls so far."""

    def __init__(self, up=True):
        self.up = up
        self.ports = []
        self.hosts = []

    def __call__(self, host, port):
        self.hosts.append(host)
        self.ports.append(port)
        return self.up(len(self.ports)) if callable(self.up) else self.up


@pytest.fixture
def paths(tmp_path):
    p = Paths(config_dir=tmp_path / "etc", data_dir=tmp_path / "var", media_root=tmp_path / "media", install_dir=tmp_path / "opt")
    for folder in (p.config_dir, p.data_dir, p.media_root, p.install_dir):
        folder.mkdir()
    return p


@pytest.fixture
def machine(paths):
    """Jobs on the fake machine, and the fakes, to look at afterwards. The background thread is run there and then."""
    runner, clock, probe = FakeRunner(), Clock(), Probe()
    host = Host(paths=paths, run=runner, owner=getpass.getuser(), group=grp.getgrgid(os.getgid()).gr_name, probe=probe, clock=clock,
                sleep=clock.sleep, monotonic=clock, background=lambda work: work())
    return Jobs(host), runner, clock, probe


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# --- the job table


def test_the_table_lists_this_tasks_jobs_and_nothing_else(machine):
    jobs, *_ = machine
    assert set(jobs.table) == {"write_config", "save_credential", "usb_files", "read_usb_file", "restart", "restart_status", "reboot",
                               "version", "journal"}


def test_a_job_refuses_an_argument_it_does_not_take(machine):
    jobs, *_ = machine
    with pytest.raises(JobError, match="does not take"):
        jobs.table["version"]({"command": "rm -rf /"})


# --- write_config


def test_check_config_yaml_gives_the_mapping():
    assert check_config_yaml(GOOD)["room"]["name"] == "Conference A"


@pytest.mark.parametrize("text, words", [
    ("room: [unclosed", "not valid YAML"),
    ("- a list\n- of things\n", "mapping"),
    ("", "mapping"),
    ("room:\n  colour: blue\n", "would not start"),
    ("meeting: [zoom]\n", "would not start"),
    (42, "text"),
])
def test_check_config_yaml_refuses_in_words(text, words):
    with pytest.raises(JobError, match=words):
        check_config_yaml(text)


def test_write_config_writes_the_text_owned_by_the_room_mode_640(machine, paths):
    jobs, *_ = machine
    jobs.table["write_config"]({"yaml": GOOD})
    config = paths.config_dir / "config.yaml"
    assert config.read_text() == GOOD
    assert mode(config) == 0o640
    assert config.owner() == getpass.getuser()
    assert [p.name for p in paths.config_dir.iterdir()] == ["config.yaml"]   # no temporary file left behind, no .1 for a first write


def test_write_config_keeps_five_older_versions(machine, paths):
    jobs, *_ = machine
    for n in range(8):
        jobs.table["write_config"]({"yaml": f"room:\n  name: Version {n}\n"})
    config = paths.config_dir / "config.yaml"
    assert yaml.safe_load(config.read_text())["room"]["name"] == "Version 7"
    for back in range(1, 6):
        older = paths.config_dir / f"config.yaml.{back}"
        assert yaml.safe_load(older.read_text())["room"]["name"] == f"Version {7 - back}"
    assert not (paths.config_dir / "config.yaml.6").exists()


def test_write_config_refuses_a_bad_config_and_writes_nothing(machine, paths):
    jobs, *_ = machine
    jobs.table["write_config"]({"yaml": GOOD})
    before = sorted(p.name for p in paths.config_dir.iterdir())
    with pytest.raises(JobError, match="would not start"):
        jobs.table["write_config"]({"yaml": "room:\n  colour: blue\n"})
    assert sorted(p.name for p in paths.config_dir.iterdir()) == before
    assert (paths.config_dir / "config.yaml").read_text() == GOOD


def test_a_key_with_text_that_cannot_be_written_is_refused_in_words(machine, paths):
    """A lone surrogate (JSON's \\ud800) reaches the helper as a str that has no UTF-8 form."""
    jobs, *_ = machine
    content = json.dumps(dict(GOOGLE_KEY, client_email="room\ud800@croom.iam.gserviceaccount.com"), ensure_ascii=False)
    with pytest.raises(JobError, match="must be text"):
        jobs.table["save_credential"]({"kind": "google", "content": content})
    assert list(paths.config_dir.iterdir()) == []


def test_write_config_needs_its_yaml(machine):
    jobs, *_ = machine
    with pytest.raises(JobError, match="text"):
        jobs.table["write_config"]({})


# --- save_credential


def test_a_google_key_is_saved_mode_600_and_summarised(machine, paths):
    jobs, *_ = machine
    answer = jobs.table["save_credential"]({"kind": "google", "content": json.dumps(GOOGLE_KEY)})
    saved = paths.config_dir / "google-service-account.json"
    assert answer == {"path": str(saved), "summary": "Key for room@croom.iam.gserviceaccount.com"}
    assert json.loads(saved.read_text()) == GOOGLE_KEY
    assert mode(saved) == 0o600
    assert saved.owner() == getpass.getuser()


def test_zoom_credentials_are_saved_mode_600(machine, paths):
    jobs, *_ = machine
    answer = jobs.table["save_credential"]({"kind": "zoom", "content": json.dumps(ZOOM)})
    saved = paths.config_dir / "zoom-credentials.json"
    assert answer["path"] == str(saved) and answer["summary"] == "Zoom Meeting SDK keys"
    assert mode(saved) == 0o600


def test_zoom_credentials_with_a_room_user_say_so(machine):
    jobs, *_ = machine
    full = dict(ZOOM, account_id="acc", s2s_client_id="id", s2s_client_secret="secret", room_user="room-a@crystalpm.com")
    answer = jobs.table["save_credential"]({"kind": "zoom", "content": json.dumps(full)})
    assert answer["summary"] == "Zoom Meeting SDK keys, joining as room-a@crystalpm.com"


def test_a_zoom_file_uploaded_as_the_google_key_is_refused_naming_what_it_is(machine, paths):
    jobs, *_ = machine
    with pytest.raises(JobError, match="Zoom credentials file, not a Google service-account key"):
        jobs.table["save_credential"]({"kind": "google", "content": json.dumps(ZOOM)})
    assert not (paths.config_dir / "google-service-account.json").exists()


def test_a_google_key_uploaded_as_zoom_credentials_is_refused_naming_what_it_is(machine):
    jobs, *_ = machine
    with pytest.raises(JobError, match="Google service-account key, not Zoom credentials"):
        jobs.table["save_credential"]({"kind": "zoom", "content": json.dumps(GOOGLE_KEY)})


@pytest.mark.parametrize("kind, content, words", [
    ("google", "not json at all", "not a JSON file"),
    ("google", "[1, 2]", "not a JSON file"),
    ("google", json.dumps(dict(GOOGLE_KEY, private_key="")), "private_key"),
    ("google", json.dumps({k: v for k, v in GOOGLE_KEY.items() if k != "client_email"}), "client_email"),
    ("google", json.dumps({"installed": {"client_id": "x"}}), "OAuth client file"),
    ("zoom", json.dumps({"sdk_client_id": "abc", "sdk_client_secret": "REPLACE_ME"}), "sdk_client_secret"),
    ("zoom", json.dumps(dict(ZOOM, account_id="acc")), "go together"),
    ("zoom", json.dumps({"hello": "world"}), "sdk_client_id"),
    ("aws", json.dumps(ZOOM), "Google service-account key or Zoom credentials"),
])
def test_save_credential_refusals(machine, paths, kind, content, words):
    jobs, *_ = machine
    with pytest.raises(JobError, match=words):
        jobs.table["save_credential"]({"kind": kind, "content": content})
    assert list(paths.config_dir.iterdir()) == []


def test_a_saved_key_replaces_the_one_before(machine, paths):
    jobs, *_ = machine
    jobs.table["save_credential"]({"kind": "google", "content": json.dumps(GOOGLE_KEY)})
    other = dict(GOOGLE_KEY, client_email="other@croom.iam.gserviceaccount.com")
    jobs.table["save_credential"]({"kind": "google", "content": json.dumps(other)})
    assert json.loads((paths.config_dir / "google-service-account.json").read_text())["client_email"] == other["client_email"]
    assert [p.name for p in paths.config_dir.iterdir()] == ["google-service-account.json"]


# --- the USB stick


@pytest.fixture
def stick(paths):
    """A stick mounted the way the desktop mounts one: /media/<user>/<label>/."""
    root = paths.media_root / "pi" / "KEYS"
    root.mkdir(parents=True)
    (root / "google.json").write_text(json.dumps(GOOGLE_KEY))
    (root / "notes.txt").write_text("not a key")
    (root / "big.json").write_text("x" * (MAX_USB_FILE + 1))
    (root / "deeper").mkdir()
    (root / "deeper" / "zoom.json").write_text(json.dumps(ZOOM))
    (paths.media_root / "pi" / "loose.json").write_text("{}")
    outside = paths.config_dir / "secret.json"
    outside.write_text('{"secret": true}')
    (root / "link.json").symlink_to(outside)
    return root


def test_usb_files_lists_only_json_files_at_the_top_of_a_stick(machine, stick):
    jobs, *_ = machine
    answer = jobs.table["usb_files"]({})
    assert answer == {"files": [{"path": str(stick / "google.json"), "name": "google.json", "stick": "KEYS",
                                 "size": len(json.dumps(GOOGLE_KEY))}]}


def test_usb_files_with_no_media_is_an_empty_list(machine, paths):
    jobs, *_ = machine
    paths.media_root.rmdir()
    assert jobs.table["usb_files"]({}) == {"files": []}


def test_read_usb_file_reads_a_listed_file(machine, stick):
    jobs, *_ = machine
    assert jobs.table["read_usb_file"]({"path": str(stick / "google.json")}) == {"content": json.dumps(GOOGLE_KEY)}


@pytest.mark.parametrize("name", ["link.json", "big.json", "notes.txt", "deeper/zoom.json", "../loose.json", "../../../etc/secret.json"])
def test_read_usb_file_refuses_what_usb_files_does_not_list(machine, stick, name):
    jobs, *_ = machine
    with pytest.raises(JobError, match="not a key file on a USB stick"):
        jobs.table["read_usb_file"]({"path": str(stick / name)})


def test_read_usb_file_refuses_any_other_path(machine, stick, paths):
    jobs, *_ = machine
    for path in ("/etc/shadow", str(paths.config_dir / "secret.json"), "", 7):
        with pytest.raises(JobError):
            jobs.table["read_usb_file"]({"path": path})


# --- restart, the health watch and its undo


def write_twice(jobs):
    """A good config, then the change the room may not start with: config.yaml.1 is the good one."""
    jobs.table["write_config"]({"yaml": GOOD})
    jobs.table["write_config"]({"yaml": "room:\n  name: Changed\ncontrol:\n  port: 8099\n"})


def test_restart_restarts_the_room_and_watches_it_come_back(machine, paths):
    jobs, runner, clock, probe = machine
    probe.up = lambda calls: calls >= 3
    write_twice(jobs)
    assert jobs.table["restart"]({}) == {"restarting": True}
    assert runner.calls == [["systemctl", "restart", "croom"]]
    assert set(probe.ports) == {8099}
    status = jobs.table["restart_status"]({})
    assert status["state"] == "healthy" and status["undone"] is False
    assert yaml.safe_load((paths.config_dir / "config.yaml").read_text())["room"]["name"] == "Changed"


def test_the_watch_asks_every_5_seconds_for_2_minutes(machine):
    jobs, runner, clock, probe = machine
    probe.up = False
    jobs.table["restart"]({})
    assert len(probe.ports) == HEALTH_TIMEOUT_S // 5 == 24
    assert probe.ports[0] == 8080   # no config: the control page's default port


def test_a_room_that_does_not_come_back_after_a_recent_change_gets_the_last_config_back(machine, paths):
    jobs, runner, clock, probe = machine
    write_twice(jobs)
    clock.sleep(9 * 60)
    # Down with the new config, up again with the one before it.
    probe.up = lambda calls: calls > HEALTH_TIMEOUT_S // 5
    jobs.table["restart"]({})
    assert runner.calls == [["systemctl", "restart", "croom"], ["systemctl", "restart", "croom"]]
    config = paths.config_dir / "config.yaml"
    assert config.read_text() == GOOD
    assert mode(config) == 0o640 and config.owner() == getpass.getuser()
    status = jobs.table["restart_status"]({})
    assert status["undone"] is True and status["state"] == "undone"
    assert "2 minutes" in status["why"] and status["at"]
    state = json.loads((paths.data_dir / "admin-state.json").read_text())
    assert state["restart"]["undone"] is True


def test_an_old_change_is_not_undone(machine, paths):
    jobs, runner, clock, probe = machine
    write_twice(jobs)
    clock.sleep(11 * 60)
    probe.up = False
    jobs.table["restart"]({})
    assert runner.calls == [["systemctl", "restart", "croom"]]
    assert yaml.safe_load((paths.config_dir / "config.yaml").read_text())["room"]["name"] == "Changed"
    status = jobs.table["restart_status"]({})
    assert status["undone"] is False and status["state"] == "down" and "did not answer" in status["why"]


def test_a_change_is_undone_once_only(machine, paths):
    jobs, runner, clock, probe = machine
    write_twice(jobs)
    probe.up = False
    jobs.table["restart"]({})
    status = jobs.table["restart_status"]({})
    assert status["undone"] is True and "not answered since" in status["why"]
    jobs.table["restart"]({})
    # The second restart has no recent change to undo: config.yaml stays the one put back, not config.yaml.1's copy of it.
    assert runner.calls.count(["systemctl", "restart", "croom"]) == 3
    assert (paths.config_dir / "config.yaml").read_text() == GOOD
    assert jobs.table["restart_status"]({})["state"] == "down"


def test_a_watch_cut_off_by_the_helper_restarting_goes_on_when_it_starts_again(machine, paths):
    jobs, runner, clock, probe = machine
    write_twice(jobs)
    jobs._host.background = lambda work: None   # the helper stops before the watch ends
    jobs.table["restart"]({})
    assert jobs.table["restart_status"]({})["state"] == "restarting"
    host = jobs._host
    host.background = lambda work: work()
    probe.up = lambda calls: calls > HEALTH_TIMEOUT_S // 5
    again = Jobs(host)
    again.resume()
    assert (paths.config_dir / "config.yaml").read_text() == GOOD
    assert again.table["restart_status"]({})["undone"] is True


def test_resume_does_nothing_after_a_watch_that_ended(machine):
    jobs, runner, clock, probe = machine
    jobs.table["restart"]({})
    calls = len(probe.ports)
    Jobs(jobs._host).resume()
    assert len(probe.ports) == calls and runner.calls == [["systemctl", "restart", "croom"]]


def test_restart_status_before_any_restart(machine):
    jobs, *_ = machine
    assert jobs.table["restart_status"]({}) == {"state": "none", "undone": False}


def test_a_restart_while_one_is_being_watched_is_refused(machine, paths):
    jobs, runner, clock, probe = machine
    jobs._host.background = lambda work: None   # the watch never ends
    jobs.table["restart"]({})
    with pytest.raises(JobError, match="restarting"):
        jobs.table["restart"]({})


# --- reboot and version


def test_reboot(machine):
    jobs, runner, clock, _ = machine
    started = clock.now
    assert jobs.table["reboot"]({}) == {"rebooting": True}
    assert runner.calls == [["systemctl", "reboot"]]
    assert clock.now - started >= 1                         # a moment after the answer


def test_a_reboot_systemctl_refuses_is_logged(machine, caplog):
    """The answer has gone by the time systemctl runs: its refusal is in the helper's log."""
    jobs, runner, *_ = machine
    runner.codes["systemctl reboot"] = 1
    assert jobs.table["reboot"]({}) == {"rebooting": True}
    assert "did not reboot" in caplog.text


class BlockingRunner(FakeRunner):
    """systemctl as it is on the room: restart croom does not return until the room software has stopped, and the room software does
    not stop while the request that asked waits for its answer."""

    def __init__(self):
        super().__init__()
        import threading
        self.release = threading.Event()
        self.entered = threading.Event()

    def __call__(self, args):
        self.entered.set()
        assert self.release.wait(10), "never released"
        return super().__call__(args)


def threaded_jobs(paths, runner):
    import threading
    threads = []

    def background(work):
        thread = threading.Thread(target=work, daemon=True)
        threads.append(thread)
        thread.start()

    clock = Clock()
    host = Host(paths=paths, run=runner, owner=getpass.getuser(), group=grp.getgrgid(os.getgid()).gr_name, probe=Probe(), clock=clock,
                sleep=clock.sleep, monotonic=clock, background=background)
    return Jobs(host), threads


def test_restart_answers_before_systemctl_runs(paths):
    runner = BlockingRunner()
    jobs, threads = threaded_jobs(paths, runner)
    assert jobs.table["restart"]({}) == {"restarting": True}          # while systemctl is still held
    assert runner.entered.wait(5) and runner.calls == []
    assert jobs.table["restart_status"]({})["state"] == "restarting"
    with pytest.raises(JobError, match="restarting"):
        jobs.table["write_config"]({"yaml": GOOD})                     # the watch has begun: no change meanwhile
    runner.release.set()
    for thread in threads:
        thread.join(5)
    assert runner.calls == [["systemctl", "restart", "croom"]]
    assert jobs.table["restart_status"]({})["state"] == "healthy"
    jobs.table["write_config"]({"yaml": GOOD})                         # and once it is over, changes are taken again


def test_reboot_answers_before_systemctl_runs(paths):
    runner = BlockingRunner()
    jobs, threads = threaded_jobs(paths, runner)
    assert jobs.table["reboot"]({}) == {"rebooting": True}
    assert runner.entered.wait(5) and runner.calls == []
    runner.release.set()
    for thread in threads:
        thread.join(5)
    assert runner.calls == [["systemctl", "reboot"]]


def test_a_restart_cut_off_before_systemctl_ran_is_watched_again(machine, paths):
    """The helper stopped between the answer and systemctl: the record says restarting, and the watch goes on at the next start."""
    jobs, runner, clock, probe = machine
    jobs._host.background = lambda work: None
    jobs.table["restart"]({})
    jobs._host.background = lambda work: work()
    Jobs(jobs._host).resume()
    assert jobs.table["restart_status"]({})["state"] == "healthy" and probe.ports


def test_version(machine, paths):
    jobs, *_ = machine
    assert jobs.table["version"]({}) == {"version": "unknown"}
    (paths.install_dir / "VERSION").write_text("fe1c05f\n")
    assert jobs.table["version"]({}) == {"version": "fe1c05f"}


# --- Task 3: the schema's check, and the root-context fixes folded in


def test_write_config_checks_every_setting_the_schema_knows(machine, paths):
    jobs, *_ = machine
    for text, words in (("room:\n  name: ''\n", "can't be blank"),
                        ("laptop:\n  gpio_pin: 99\n", "from 0 to 27"),
                        ("calendar:\n  booking_start: '18:00'\n  booking_end: '09:00'\n", "must be after")):
        with pytest.raises(JobError, match=words):
            jobs.table["write_config"]({"yaml": text})
    assert list(paths.config_dir.iterdir()) == []


def test_write_config_refuses_turning_the_room_page_off(machine, paths):
    jobs, *_ = machine
    with pytest.raises(JobError, match="can't be turned off"):
        jobs.table["write_config"]({"yaml": "control:\n  enabled: false\n"})
    assert list(paths.config_dir.iterdir()) == []


@pytest.mark.parametrize("change", ["control:\n  port: 9000\n", "room:\n  name: A\n", "control:\n  port: 8099\n  host: 10.0.0.9\n"])
def test_write_config_refuses_moving_the_room_page_from_the_config_on_disk(machine, paths, change):
    """GOOD's room page is on 8099 at every address: a config that leaves out the port (8080) or names another host moves it."""
    jobs, *_ = machine
    jobs.table["write_config"]({"yaml": GOOD})
    with pytest.raises(JobError, match="can't be changed from Settings"):
        jobs.table["write_config"]({"yaml": change})
    assert (paths.config_dir / "config.yaml").read_text() == GOOD


def test_write_config_is_refused_while_a_restart_is_watched(machine, paths):
    jobs, *_ = machine
    jobs.table["write_config"]({"yaml": GOOD})
    jobs._host.background = lambda work: None   # the watch never ends
    jobs.table["restart"]({})
    with pytest.raises(JobError, match="The room is restarting: try again in a moment"):
        jobs.table["write_config"]({"yaml": "room:\n  name: Later\ncontrol:\n  port: 8099\n"})
    assert (paths.config_dir / "config.yaml").read_text() == GOOD


def test_the_state_file_is_mode_644_and_a_failed_write_leaves_no_temporary_file(machine, paths, monkeypatch):
    jobs, *_ = machine
    jobs.table["restart"]({})
    assert mode(paths.data_dir / "admin-state.json") == 0o644
    before = (paths.data_dir / "admin-state.json").read_text()

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("croom.admin.jobs.json.dump", broken)
    with pytest.raises(OSError):
        jobs._update_state(last_write=1)
    assert [p.name for p in paths.data_dir.iterdir()] == ["admin-state.json"]
    assert (paths.data_dir / "admin-state.json").read_text() == before


def test_the_watchs_two_minutes_are_on_the_monotonic_clock(paths):
    """The wall clock set back an hour at every look (an NTP step, a wrong RTC) neither stretches the watch nor ends it early."""
    runner, mono, probe = FakeRunner(), Clock(), Probe(up=False)
    wall = Clock()

    def sleep(seconds):
        mono.sleep(seconds)
        wall.now -= 3600
        assert len(probe.ports) < 100, "the watch did not end"

    host = Host(paths=paths, run=runner, owner=getpass.getuser(), group=grp.getgrgid(os.getgid()).gr_name, probe=probe, clock=wall,
                sleep=sleep, monotonic=mono, background=lambda work: work())
    Jobs(host).table["restart"]({})
    assert len(probe.ports) == HEALTH_TIMEOUT_S // 5


@pytest.mark.parametrize("host, asked", [("192.168.1.20", "192.168.1.20"), ("0.0.0.0", "127.0.0.1"), ("::", "127.0.0.1"), ("", "127.0.0.1"),
                                         (None, "127.0.0.1")])
def test_the_watch_asks_the_room_page_where_it_listens(machine, paths, host, asked):
    jobs, runner, clock, probe = machine
    control = {"port": 8099} if host is None else {"port": 8099, "host": host}
    (paths.config_dir / "config.yaml").write_text(yaml.safe_dump({"control": control}))
    jobs.table["restart"]({})
    assert set(probe.hosts) == {asked} and set(probe.ports) == {8099}


def test_status_answers_puts_an_ipv6_host_in_brackets(monkeypatch):
    from croom.admin import jobs as module
    asked = []

    def urlopen(url, timeout):
        asked.append(url)
        raise OSError("refused")

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    assert module.status_answers("fd00::5", 8080) is False
    assert module.status_answers("10.0.0.2", 8081) is False
    assert asked == ["http://[fd00::5]:8080/api/status", "http://10.0.0.2:8081/api/status"]


def test_read_usb_file_refuses_what_is_not_a_regular_file_without_waiting_on_it(machine, stick, monkeypatch):
    """A FIFO put in a listed file's place: opened without blocking and refused, not read (a read would hold the helper for good)."""
    jobs, *_ = machine
    fifo = stick / "pipe.json"
    os.mkfifo(fifo)
    monkeypatch.setattr(jobs, "_usb_listing", lambda: [{"path": str(fifo)}])
    with pytest.raises(JobError, match="not a key file on a USB stick"):
        jobs.table["read_usb_file"]({"path": str(fifo)})


# --- journal (Settings > Logs): journalctl -u croom -o json, newest first, read by a fake runner

def journal_line(text, priority=None, at_us=1_800_000_000_000_000, cursor="s=ab;i=1", **more):
    record = {"MESSAGE": text, "__REALTIME_TIMESTAMP": str(at_us), "__CURSOR": cursor, **more}
    if priority is not None:
        record["PRIORITY"] = str(priority)
    return json.dumps(record)


class JournalRunner:
    """journalctl: what it was asked, and the lines it prints (`code` other than 0: it fails, printing `output`)."""

    def __init__(self, lines=(), code=0, output=""):
        self.calls, self.lines, self.code, self.output = [], list(lines), code, output

    def __call__(self, args):
        self.calls.append(list(args))
        return (self.code, self.output) if self.code else (0, "\n".join(self.lines) + "\n")


def journal_jobs(paths, runner):
    return Jobs(Host(paths=paths, run=runner, owner=getpass.getuser(), group=grp.getgrgid(os.getgid()).gr_name))


def test_journal_reads_the_newest_lines_of_the_room_software_with_journalctl_and_no_shell(paths):
    from datetime import datetime
    runner = JournalRunner([
        journal_line("2026-10-30 10:42:13,512 - croom.tv.service - WARNING - The TV is not answering", 4, cursor="s=ab;i=3"),
        journal_line("Started croom.service - Crystal Meet room agent.", 6, at_us=1_799_999_999_000_000, cursor="s=ab;i=2"),
    ])
    answer = journal_jobs(paths, runner).journal({})
    assert runner.calls == [["journalctl", "--unit=croom", "--output=json", "--reverse", "--lines=200", "--no-pager", "--quiet"]]
    when = datetime.fromtimestamp(1_800_000_000).astimezone().isoformat(timespec="seconds")
    assert answer["entries"][0] == {"time": when, "level": "warning", "text": "croom.tv.service - The TV is not answering"}
    assert answer["entries"][1]["text"] == "Started croom.service - Crystal Meet room agent." and answer["entries"][1]["level"] == "info"
    assert answer["cursor"] == "s=ab;i=2"                      # the oldest shown: Load more begins after it
    assert answer["more"] is False                             # fewer than asked: the log has no more


def test_journal_warnings_and_errors_and_the_page_before_a_cursor(paths):
    runner = JournalRunner([journal_line("x", 3, cursor=f"s=ab;i={n}") for n in range(5, 0, -1)])
    answer = journal_jobs(paths, runner).journal({"lines": 5, "level": "warning", "before": "s=ab;i=9;b=00;m=1f;t=5d8;x=91"})
    assert runner.calls[0][-2:] == ["--priority=warning", "--after-cursor=s=ab;i=9;b=00;m=1f;t=5d8;x=91"]   # one argument each: never an option of its own
    assert "--lines=5" in runner.calls[0]
    assert [e["level"] for e in answer["entries"]] == ["error"] * 5 and answer["more"] is True and answer["cursor"] == "s=ab;i=1"


@pytest.mark.parametrize("priority, text, level", [
    (0, "kernel panic", "critical"), (2, "x", "critical"), (3, "x", "error"), (4, "x", "warning"), (5, "x", "info"), (7, "x", "debug"),
    # a line from before the agent wrote its priorities: the journal has it at info, and the line says what it is
    (6, "2026-10-30 10:42:13,512 - croom.x - ERROR - Boom", "error"),
    (None, "2026-10-30 10:42:13,512 - croom.x - CRITICAL - Boom", "critical"),
    # the agent's priority wins over the words
    (4, "2026-10-30 10:42:13,512 - croom.x - INFO - says info", "warning"),
    (6, "Traceback (most recent call last):", "info"),
])
def test_journal_levels(paths, priority, text, level):
    runner = JournalRunner([journal_line(text, priority)])
    assert journal_jobs(paths, runner).journal({})["entries"][0]["level"] == level


def test_journal_lines_that_are_not_plain_text(paths):
    from croom.admin.jobs import MAX_JOURNAL_TEXT, TOO_LONG_FOR_THE_JOURNAL
    long = "y" * (MAX_JOURNAL_TEXT + 50)
    runner = JournalRunner([
        json.dumps({"MESSAGE": list("café".encode("utf-8")) + [0xff], "PRIORITY": "6", "__CURSOR": "s=1"}),   # not UTF-8: its bytes
        json.dumps({"MESSAGE": None, "PRIORITY": "3", "__CURSOR": "s=2"}),                                         # over the JSON output's limit
        journal_line(long, 6),
        "not json", "[1, 2]", "",
        json.dumps({"MESSAGE": "no time", "__REALTIME_TIMESTAMP": "soon"}),
        json.dumps({"MESSAGE": list(b"\x1b[33mWARNING\x1b[0m: low\x07 disk\tspace"), "__CURSOR": "s=3"}),   # a terminal's colours
    ])
    entries = journal_jobs(paths, runner).journal({})["entries"]
    assert entries[0]["text"] == "café�" and entries[1]["text"] == TOO_LONG_FOR_THE_JOURNAL
    assert len(entries[2]["text"]) == MAX_JOURNAL_TEXT and entries[2]["text"].endswith("…")
    assert len(entries) == 5 and entries[3] == {"time": "", "level": "info", "text": "no time"}
    assert entries[4]["text"] == "WARNING: low disk\tspace"


@pytest.mark.parametrize("args, words", [
    ({"lines": 0}, "The lines must be a whole number from 1 to 500"),
    ({"lines": 501}, "The lines must be a whole number from 1 to 500"),
    ({"lines": "200"}, "The lines must be a whole number from 1 to 500"),
    ({"lines": True}, "The lines must be a whole number from 1 to 500"),
    ({"level": "debug"}, 'The level must be "all" or "warning"'),
    ({"before": "--since=yesterday"}, "That is not a place in the log"),
    ({"before": "s=1 i=2"}, "That is not a place in the log"),
    ({"before": 5}, "That is not a place in the log"),
    ({"unit": "ssh"}, "This job does not take unit"),
])
def test_journal_refuses_what_it_does_not_take_without_running_anything(paths, args, words):
    runner = JournalRunner()
    with pytest.raises(JobError) as refused:
        journal_jobs(paths, runner).journal(args)
    assert str(refused.value) == words and runner.calls == []


def test_journal_says_why_it_could_not_read_the_log(paths):
    runner = JournalRunner(code=1, output="Failed to seek to cursor: Invalid argument\n")
    with pytest.raises(JobError) as refused:
        journal_jobs(paths, runner).journal({"before": "s=1"})
    assert str(refused.value) == "The log could not be read: Failed to seek to cursor: Invalid argument"
