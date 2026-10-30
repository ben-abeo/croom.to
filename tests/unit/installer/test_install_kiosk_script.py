"""
Tests for installer/install-kiosk.sh, the table Pi's one-command setup: the room
page full screen in Chromium's kiosk mode, started at login and from a desktop
icon. Nothing here needs root, apt or a Pi.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "installer" / "install-kiosk.sh"


def run_bash(snippet, env=None):
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=merged, cwd=REPO)


def me():
    return subprocess.check_output(["id", "-un"], text=True).strip()


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_help_mentions_the_url_option():
    result = run_bash(f"bash {SCRIPT} --help")
    assert result.returncode == 0
    assert "--url" in result.stdout


def test_refuses_to_run_without_a_url():
    result = run_bash(f"bash {SCRIPT}")
    assert result.returncode == 1
    assert "--url" in (result.stdout + result.stderr)


def test_kiosk_command_opens_the_room_page_full_screen_with_the_keyboard_flag(tmp_path):
    bin_dir = tmp_path / "bin"
    result = run_bash(f"source {SCRIPT}; BIN_DIR={bin_dir}; KIOSK_URL=http://pimeet-3.local:8080/; write_kiosk_command")
    assert result.returncode == 0, result.stderr
    command = bin_dir / "crystal-meet-kiosk"
    text = command.read_text()
    assert os.access(command, os.X_OK)
    assert "--kiosk" in text and "http://pimeet-3.local:8080/?keyboard=1" in text
    assert "--force-device-scale-factor=1.5" in text   # the 1920x1200 touch panel lays the page out at 1280x800
    assert "--disable-session-crashed-bubble" in text and "--hide-crash-restore-bubble" in text
    assert "pgrep" in text  # already open: do nothing
    assert "curl" in text   # waits for the room page before opening the browser
    # Re-running with a new address replaces it.
    result = run_bash(f"source {SCRIPT}; BIN_DIR={bin_dir}; KIOSK_URL=http://10.0.0.7:8080/; write_kiosk_command")
    assert result.returncode == 0, result.stderr
    text = command.read_text()
    assert "http://10.0.0.7:8080/?keyboard=1" in text and "pimeet-3" not in text


def test_keyboard_flag_is_appended_to_an_address_that_already_has_a_query(tmp_path):
    bin_dir = tmp_path / "bin"
    run_bash(f"source {SCRIPT}; BIN_DIR={bin_dir}; KIOSK_URL='http://10.0.0.7:8080/?lang=en'; write_kiosk_command")
    assert "http://10.0.0.7:8080/?lang=en&keyboard=1" in (bin_dir / "crystal-meet-kiosk").read_text()


def test_autostart_line_is_added_once(tmp_path):
    home = tmp_path / "home"
    for _ in range(2):
        result = run_bash(f"source {SCRIPT}; KIOSK_HOME={home}; KIOSK_USER={me()}; BIN_DIR=/usr/local/bin; write_autostart")
        assert result.returncode == 0, result.stderr
    lines = (home / ".config" / "labwc" / "autostart").read_text().splitlines()
    assert lines.count("/usr/local/bin/crystal-meet-kiosk &") == 1


def test_desktop_icon_reopens_the_room_page(tmp_path):
    home = tmp_path / "home"
    result = run_bash(f"source {SCRIPT}; KIOSK_HOME={home}; KIOSK_USER={me()}; BIN_DIR=/usr/local/bin; write_desktop_icon")
    assert result.returncode == 0, result.stderr
    entry = home / "Desktop" / "room-controls.desktop"
    text = entry.read_text()
    assert "[Desktop Entry]" in text and "Name=Room controls" in text and "Type=Application" in text
    assert "Exec=/usr/local/bin/crystal-meet-kiosk" in text and "Terminal=false" in text
    assert os.access(entry, os.X_OK)
    icon = home / ".local" / "share" / "icons" / "crystal-meet.svg"
    assert icon.read_text().lstrip().startswith("<svg")
    assert f"Icon={icon}" in text


def test_embedded_icon_is_the_packaged_one():
    """The kiosk Pi has no Crystal Meet package, so the script carries the icon; keep it in sync."""
    packaged = (REPO / "src" / "croom" / "control" / "static" / "crystal-meet.svg").read_text().strip()
    assert packaged in SCRIPT.read_text()


# --- The touch Pi's agent (room settings spec 2026-10-29, section 7): a root service run from a root-owned copy of this checkout's commit

FAKE_SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "$CALLS"
"""


def group():
    return subprocess.check_output(["id", "-gn"], text=True).strip()


class KioskPi:
    """A clone of the repository (two files, one commit) and the touch Pi's folders, in a temporary folder; systemctl is a stand-in
    on PATH that writes down what it is asked."""

    def __init__(self, root):
        self.root = root
        self.source = root / "croom.to"
        (self.source / "installer").mkdir(parents=True)
        (self.source / "tools" / "kiosk_agent").mkdir(parents=True)
        (self.source / "installer" / "install-kiosk.sh").write_text("# the kiosk setup\n")
        (self.source / "tools" / "kiosk_agent" / "croom_kiosk_agent.py").write_text("# the agent\n")
        os.chmod(self.source / "installer" / "install-kiosk.sh", 0o664)
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"],
                     ["add", "."], ["commit", "-q", "-m", "one"]):
            subprocess.run(["git", *args], cwd=self.source, check=True, capture_output=True)
        self.commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.source, text=True).strip()
        self.opt, self.state, self.units = root / "opt", root / "state", root / "units"
        self.bin = root / "bin"
        self.bin.mkdir()
        (self.bin / "systemctl").write_text(FAKE_SYSTEMCTL)
        os.chmod(self.bin / "systemctl", 0o755)
        self.calls = root / "calls"
        self.calls.write_text("")

    def run(self, snippet="install_agent", **env):
        settings = {"KIOSK_SOURCE": str(self.source), "AGENT_DIR": str(self.opt), "AGENT_STATE": str(self.state),
                    "UNIT_DIR": str(self.units), "RELEASE_OWNER": f"{me()}:{group()}", "KIOSK_URL": "http://pimeet-3.local:8080/",
                    "KIOSK_USER": me(), "SKIP_PACKAGES": "1", "CALLS": str(self.calls),
                    "PATH": f"{self.bin}:{os.environ['PATH']}"}
        settings.update(env)
        return run_bash(f"source {SCRIPT}; {snippet}", env=settings)

    def systemctl(self):
        return self.calls.read_text().splitlines()


def test_the_agent_is_installed_from_a_root_owned_copy_of_this_checkout(tmp_path):
    pi = KioskPi(tmp_path)
    result = pi.run()
    assert result.returncode == 0, result.stderr
    release = pi.opt / "releases" / pi.commit
    assert (release / "VERSION").read_text().strip() == pi.commit
    assert (release / "tools" / "kiosk_agent" / "croom_kiosk_agent.py").read_text() == "# the agent\n"
    for path in [release, *release.rglob("*")]:
        assert not os.lstat(path).st_mode & 0o022, path          # read-only to all but its owner (root on the Pi)
    assert os.readlink(pi.opt / "current") == f"releases/{pi.commit}" and os.readlink(pi.opt / "good") == f"releases/{pi.commit}"
    assert not list((pi.opt / "releases").glob(".partial-*")) and not list(pi.opt.glob(".*.tar"))
    # Its own clone, for updates from GitHub
    origin = subprocess.check_output(["git", "-C", str(pi.opt / "repo.git"), "remote", "get-url", "origin"], text=True).strip()
    assert origin == "https://github.com/ben-abeo/croom.to.git"
    assert subprocess.check_output(["git", "-C", str(pi.opt / "repo.git"), "rev-parse", "--is-bare-repository"], text=True).strip() == "true"
    # What it needs to know, for root alone
    config = pi.state / "config.json"
    assert json.loads(config.read_text()) == {"url": "http://pimeet-3.local:8080/", "user": me()}
    assert stat.S_IMODE(os.stat(config).st_mode) == 0o600 and stat.S_IMODE(os.stat(pi.state).st_mode) == 0o700
    # The service, from the good release
    unit = (pi.units / "croom-kiosk-agent.service").read_text()
    assert (f"ExecStart=/usr/bin/python3 {pi.opt}/good/tools/kiosk_agent/croom_kiosk_agent.py --base {pi.opt} --state {pi.state}"
            in unit)
    assert "Restart=always" in unit and "User=" not in unit and "WantedBy=multi-user.target" in unit
    assert pi.systemctl() == ["systemctl daemon-reload", "systemctl enable croom-kiosk-agent.service",
                              "systemctl restart croom-kiosk-agent.service"]


def test_running_the_installer_again_keeps_the_release_and_ends_an_update_record(tmp_path):
    pi = KioskPi(tmp_path)
    assert pi.run().returncode == 0
    (pi.state / "update.json").write_text('{"state": "trial"}')
    (pi.state / "identity.json").write_text("{}")
    marker = pi.opt / "releases" / pi.commit / "kept"
    marker.write_text("x")
    result = pi.run()
    assert result.returncode == 0, result.stderr
    assert marker.exists() and not (pi.state / "update.json").exists()
    assert (pi.state / "identity.json").exists()                  # the touch Pi stays the device the room let in


def test_run_by_the_agent_it_writes_the_unit_and_leaves_the_agent_running(tmp_path):
    pi = KioskPi(tmp_path)
    result = pi.run(KIOSK_FROM_AGENT="1")
    assert result.returncode == 0, result.stderr
    assert (pi.units / "croom-kiosk-agent.service").exists()
    assert pi.systemctl() == ["systemctl daemon-reload", "systemctl enable croom-kiosk-agent.service"]
    assert not pi.opt.exists() and not pi.state.exists()


def test_skip_agent_installs_nothing(tmp_path):
    pi = KioskPi(tmp_path)
    assert pi.run(SKIP_AGENT="1").returncode == 0
    assert pi.systemctl() == [] and not pi.opt.exists()


def test_outside_a_clone_it_says_so(tmp_path):
    pi = KioskPi(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    result = pi.run(KIOSK_SOURCE=str(plain), GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert result.returncode == 1 and "clone of the repository" in result.stderr
    assert pi.systemctl() == []


def test_the_whole_setup_installs_the_agent_and_says_how_to_let_it_in():
    text = SCRIPT.read_text()
    body = text[text.index("main() {"):text.index("run_installer() {")]
    assert body.index("disable_blanking") < body.index("install_agent") < body.index("print_completion")
    assert "tap Allow" in text


def test_the_agent_runs_with_the_system_python(tmp_path):
    """The agent is standard library only: it answers --help with a python3 that has none of the room software's packages."""
    agent = REPO / "tools" / "kiosk_agent" / "croom_kiosk_agent.py"
    result = subprocess.run(["python3", "-I", "-S", str(agent), "--help"], capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "--trial" in result.stdout
