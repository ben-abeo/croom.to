"""
installer/install.sh's release layout on the TV Pi (room settings spec 2026-10-29, section 6): releases/<commit>/venv, the venv a link to
the running one, VERSION per release and beside the link; the first run moves a venv made before releases into one; two releases kept.
The commands it runs are stand-ins on PATH (python3 -m venv makes a venv of fake pip and playwright; git ls-remote names main's newest
commit); every other python3 is the real one. Nothing here needs root, the network or GitHub.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "installer" / "install.sh"
A = "a" * 40
B = "b" * 40
P = "c" * 40
REAL_PYTHON = shutil.which("python3") or sys.executable

FAKE_PYTHON = """#!/bin/bash
if [[ "$1" == "-m" && "$2" == "venv" ]]; then
    mkdir -p "$3/bin" "$3/lib/python3.11/site-packages"
    for tool in pip playwright; do
        cat > "$3/bin/$tool" << 'TOOL'
#!/bin/bash
echo "$(basename "$0") $*" >> "$CALLS"
if [[ "$(basename "$0")" == pip && "$1" == install && -n "${PIP_COMMIT:-}" && "$2" != --upgrade ]]; then
    folder="$(dirname "$(dirname "$0")")/lib/python3.11/site-packages/croom-2.0.0.dev0.dist-info"
    mkdir -p "$folder"
    echo "{\\"url\\": \\"x\\", \\"vcs_info\\": {\\"vcs\\": \\"git\\", \\"commit_id\\": \\"$PIP_COMMIT\\"}}" > "$folder/direct_url.json"
fi
TOOL
        chmod 755 "$3/bin/$tool"
    done
    echo "python3 -m venv $3" >> "$CALLS"
    exit 0
fi
exec REAL_PYTHON "$@"
""".replace("REAL_PYTHON", REAL_PYTHON)

FAKE_GIT = """#!/bin/bash
echo "git $*" >> "$CALLS"
if [[ "$1" == ls-remote ]]; then printf '%s\\trefs/heads/main\\n' "$MAIN"; fi
"""


class Device:
    """A TV Pi's folders in a temporary one, and the stand-ins: `install()` runs install_croom as the installer does."""

    def __init__(self, root):
        self.root = root
        self.opt, self.data = root / "opt", root / "lib"
        (self.opt / "browsers").mkdir(parents=True)              # as create_directories leaves it
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, text in (("python3", FAKE_PYTHON), ("git", FAKE_GIT)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.calls = root / "calls.log"
        self.calls.touch()
        self.user = subprocess.check_output(["id", "-un"], text=True).strip()
        self.group = subprocess.check_output(["id", "-gn"], text=True).strip()

    def run(self, function, main=B, repo=None, pip_commit=""):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", CALLS=str(self.calls), MAIN=main, PIP_COMMIT=pip_commit)
        snippet = (f"source {SCRIPT}; INSTALL_DIR={self.opt}; DATA_DIR={self.data}; CROOM_USER={self.user}; "
                   f"RELEASE_OWNER={self.user}:{self.group}; " + (f"CROOM_REPO={repo}; " if repo else "") + function)
        result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=env, cwd=REPO)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    def install(self, **kwargs):
        return self.run("install_croom", **kwargs)

    def link(self):
        return os.readlink(self.opt / "venv")

    def releases(self):
        return sorted(p.name for p in (self.opt / "releases").iterdir())

    def logged(self):
        return self.calls.read_text().splitlines()

    def release(self, name, commit=None):
        venv = self.opt / "releases" / name / "venv"
        (venv / "bin").mkdir(parents=True)
        (self.opt / "releases" / name / "VERSION").write_text((commit or name) + "\n")

    def old_venv(self, commit=None):
        """The venv an installer made before releases: a folder, with pip's record of the commit it installed."""
        venv = self.opt / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "playwright").write_text("#!/bin/bash\necho \"playwright $*\" >> \"$CALLS\"\n")
        (venv / "bin" / "playwright").chmod(0o755)
        if commit:
            info = venv / "lib" / "python3.11" / "site-packages" / "croom-2.0.0.dev0.dist-info"
            info.mkdir(parents=True)
            (info / "direct_url.json").write_text(json.dumps({"url": "https://github.com/ben-abeo/croom.to.git",
                                                               "vcs_info": {"vcs": "git", "commit_id": commit}}))
        return venv

    def state(self):
        return json.loads((self.data / "admin-state.json").read_text())


@pytest.fixture
def device(tmp_path):
    return Device(tmp_path)


def test_a_first_install_builds_mains_newest_commit_as_a_release_and_links_the_venv_to_it(device):
    device.install()
    venv = device.opt / "releases" / B / "venv"
    assert device.link() == f"releases/{B}/venv"                                    # relative: the link survives a moved folder
    assert (device.opt / "releases" / B / "VERSION").read_text() == B + "\n"
    assert (device.opt / "VERSION").read_text() == B + "\n"
    logged = device.logged()
    assert f"python3 -m venv {venv}" in logged
    assert f"pip install git+https://github.com/ben-abeo/croom.to.git@{B}" in logged
    assert "playwright install chromium" in logged
    assert logged[0] == "git ls-remote https://github.com/ben-abeo/croom.to.git refs/heads/main"
    assert not (device.data / "admin-state.json").exists()                          # nothing kept before it
    mode = (device.opt / "releases" / B).stat().st_mode & 0o777
    assert mode == 0o755                                                            # readable by all, writable by its owner (root) only


def test_the_first_run_moves_a_venv_made_before_releases_into_one_and_keeps_it_as_the_previous(device):
    device.old_venv(commit=A)
    device.install()
    assert device.releases() == [A, B]
    assert (device.opt / "releases" / A / "venv" / "bin" / "playwright").exists()  # moved, not rebuilt
    assert (device.opt / "releases" / A / "VERSION").read_text() == A + "\n"
    assert device.link() == f"releases/{B}/venv"
    assert device.state()["previous_release"] == A                                  # Settings > Software can switch back to it


def test_a_moved_venv_whose_commit_pip_did_not_record_is_unknown(device):
    device.old_venv()
    device.run("migrate_venv")
    assert device.releases() == ["unknown"] and device.link() == "releases/unknown/venv"
    assert (device.opt / "releases" / "unknown" / "VERSION").read_text() == "unknown\n"
    assert (device.opt / "VERSION").read_text() == "unknown\n"


def test_migrating_keeps_the_services_paths_working_through_the_link(device):
    device.old_venv(commit=A)
    device.run("migrate_venv")
    assert (device.opt / "venv" / "bin" / "playwright").exists()                    # /opt/croom/venv/bin/... as the units name it
    device.run("migrate_venv")                                                       # a second run finds the link and does nothing
    assert device.releases() == [A] and device.link() == f"releases/{A}/venv"


def test_running_it_again_on_the_newest_commit_builds_nothing(device):
    device.install()
    device.calls.write_text("")
    device.install()
    assert not [line for line in device.logged() if line.startswith(("python3 -m venv", "pip install"))]
    assert device.link() == f"releases/{B}/venv"


def test_two_releases_are_kept(device):
    device.release(P)
    device.release(A)
    os.symlink(f"releases/{A}/venv", device.opt / "venv")
    device.install()
    assert device.releases() == [A, B] and device.link() == f"releases/{B}/venv"
    assert device.state()["previous_release"] == A


def test_another_source_is_a_release_of_its_own_with_the_commit_pip_recorded(device):
    device.install(repo="git+https://example.com/fork.git", pip_commit=P)
    name = device.link().split("/")[1]
    assert name.startswith("local-") and name[6:].isdigit()
    assert (device.opt / "releases" / name / "VERSION").read_text() == P + "\n"
    assert "pip install git+https://example.com/fork.git" in device.logged()
    assert not [line for line in device.logged() if line.startswith("git ls-remote")]


def test_the_install_folder_is_roots_and_only_its_data_folders_the_users(tmp_path):
    script = SCRIPT.read_text()
    assert 'RELEASE_OWNER="${RELEASE_OWNER:-root:root}"' in script
    directories = script.split("create_directories() {")[1].split("\n}\n")[0]
    assert 'chown -R "$CROOM_USER:$CROOM_USER" "$INSTALL_DIR"\n' not in directories
    assert 'chown "$RELEASE_OWNER" "$INSTALL_DIR"' in directories
    user = subprocess.check_output(["id", "-un"], text=True).strip()
    group = subprocess.check_output(["id", "-gn"], text=True).strip()
    result = subprocess.run(["bash", "-c", f"source {SCRIPT}; INSTALL_DIR={tmp_path / 'opt'}; CONFIG_DIR={tmp_path / 'etc'}; "
                             f"DATA_DIR={tmp_path / 'lib'}; LOG_DIR={tmp_path / 'log'}; CROOM_USER={user}; "
                             f"RELEASE_OWNER={user}:{group}; create_directories"], capture_output=True, text=True, cwd=REPO)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "opt").stat().st_mode & 0o777 == 0o755
    assert (tmp_path / "opt" / "models").is_dir() and (tmp_path / "opt" / "browsers").is_dir()


def test_the_services_run_from_the_venv_link():
    script = SCRIPT.read_text()
    assert "ExecStart=$INSTALL_DIR/venv/bin/python -m croom.core.agent" in script
    assert "ExecStart=$INSTALL_DIR/venv/bin/python -m croom.admin" in script
