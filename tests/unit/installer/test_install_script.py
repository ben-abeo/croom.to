"""
Tests for installer/install.sh that do not need root or apt: argument
handling, the desktop-user check, and config creation with the script sourced.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "installer" / "install.sh"


def run_bash(snippet, env=None):
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=merged, cwd=REPO)


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_help_exits_zero_and_mentions_config():
    result = run_bash(f"bash {SCRIPT} --help")
    assert result.returncode == 0
    assert "--config" in result.stdout


def test_missing_config_file_is_refused_before_install(tmp_path):
    result = run_bash(f"bash {SCRIPT} --config {tmp_path / 'nope.yaml'}")
    assert result.returncode == 1
    assert "not found" in (result.stdout + result.stderr).lower()


def test_requires_a_desktop_user():
    result = run_bash(f"source {SCRIPT}; CROOM_USER= check_desktop_user")
    assert result.returncode == 1
    assert "sudo" in (result.stdout + result.stderr).lower()
    result = run_bash(f"source {SCRIPT}; CROOM_USER=root check_desktop_user")
    assert result.returncode == 1


def test_default_config_has_the_control_section(tmp_path):
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={tmp_path}; CROOM_USER=$(id -un); create_config",
    )
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "config.yaml").read_text()
    assert "control:" in text and "port: 8080" in text
    assert "platforms:" in text and "- zoom" in text and "- google_meet" in text
    assert "enabled: false" in text.split("ai:")[1].split("\n\n")[0]


def test_prepared_config_is_installed_verbatim(tmp_path):
    room = tmp_path / "room.yaml"
    room.write_text("version: 2\nroom:\n  name: Room 9\n")
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={tmp_path / 'etc'}; CROOM_USER=$(id -un); ROOM_CONFIG={room}; create_config",
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "etc" / "config.yaml").read_text() == room.read_text()


def test_installs_from_the_fork_by_default():
    text = SCRIPT.read_text()
    assert 'CROOM_REPO="${CROOM_REPO:-git+https://github.com/ben-abeo/croom.to.git}"' in text
    assert "pip\" install croom" not in text


def test_config_option_without_a_value_is_refused():
    result = run_bash(f"bash {SCRIPT} --config")
    assert result.returncode == 1
    assert "not found" in (result.stdout + result.stderr).lower()


def test_desktop_user_must_exist():
    result = run_bash(f"source {SCRIPT}; CROOM_USER=no_such_user_for_croom check_desktop_user")
    assert result.returncode == 1
    assert "does not exist" in (result.stdout + result.stderr).lower()


def test_units_run_as_the_desktop_user_with_the_bundled_browser(tmp_path):
    result = run_bash(f"source {SCRIPT}; SYSTEMD_DIR={tmp_path}; CROOM_USER=$(id -un); write_units")
    assert result.returncode == 0, result.stderr
    me = subprocess.check_output(["id", "-un"], text=True).strip()
    unit = (tmp_path / "croom.service").read_text()
    assert f"User={me}" in unit
    assert "Description=Crystal Meet room agent (croom)" in unit
    assert "Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/croom/browsers" in unit
    assert f"Environment=XDG_RUNTIME_DIR=/run/user/{os.getuid()}" in unit
    # The agent opens a headed browser: start after the display manager and wait for the display.
    assert "After=display-manager.service" in unit
    assert "ExecStartPre=" in unit and "/tmp/.X11-unix/X0" in unit
    ui = (tmp_path / "croom-ui.service").read_text()
    assert f"User={me}" in ui and "Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/croom/browsers" in ui


def test_browser_is_installed_for_the_service_user():
    install = SCRIPT.read_text().split("install_croom() {")[1].split("\n}\n")[0]
    assert 'export PLAYWRIGHT_BROWSERS_PATH="$INSTALL_DIR/browsers"' in install
    assert install.index("PLAYWRIGHT_BROWSERS_PATH") < install.index('playwright" install chromium')
    assert 'chown -R "$CROOM_USER:$CROOM_USER" "$INSTALL_DIR/browsers"' in install


def test_completion_message_skips_editing_when_a_room_config_was_installed():
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG=/tmp/room.yaml; print_completion")
    assert result.returncode == 0, result.stderr
    assert "Edit configuration" not in result.stdout and "Room page:" in result.stdout
    result = run_bash(f"source {SCRIPT}; print_completion")
    assert "Edit configuration" in result.stdout
