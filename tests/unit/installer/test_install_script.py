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


def test_missing_credentials_file_is_refused_before_install(tmp_path):
    result = run_bash(f"bash {SCRIPT} --credentials {tmp_path / 'nope.json'}")
    assert result.returncode == 1
    assert "not found" in (result.stdout + result.stderr).lower()


def test_help_mentions_credentials():
    result = run_bash(f"bash {SCRIPT} --help")
    assert "--credentials FILE" in result.stdout


def test_credentials_are_installed_for_the_service_user_only(tmp_path):
    key = tmp_path / "key.json"
    key.write_text('{"type": "service_account"}')
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={tmp_path / 'etc'}; CROOM_USER=$(id -un); CREDENTIALS_FILE={key}; install_credentials",
    )
    assert result.returncode == 0, result.stderr
    installed = tmp_path / "etc" / "google-service-account.json"
    assert installed.read_text() == key.read_text()
    assert oct(installed.stat().st_mode & 0o777) == "0o600"


def test_install_credentials_does_nothing_without_a_file(tmp_path):
    result = run_bash(f"source {SCRIPT}; CONFIG_DIR={tmp_path / 'etc'}; CROOM_USER=$(id -un); install_credentials")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "etc" / "google-service-account.json").exists()


def test_completion_message_names_the_calendar_check_when_credentials_were_installed():
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG=/tmp/room.yaml; CREDENTIALS_FILE=/tmp/key.json; print_completion")
    assert result.returncode == 0, result.stderr
    assert "croom --check-calendar -c /etc/croom/config.yaml" in result.stdout
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG=/tmp/room.yaml; print_completion")
    assert "--check-calendar" not in result.stdout


def test_reinstalling_with_the_installed_key_path_keeps_it(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    installed = etc / "google-service-account.json"
    installed.write_text('{"type": "service_account"}')
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={etc}; CROOM_USER=$(id -un); CREDENTIALS_FILE={installed}; install_credentials",
    )
    assert result.returncode == 0, result.stderr
    assert installed.read_text() == '{"type": "service_account"}'
    assert oct(installed.stat().st_mode & 0o777) == "0o600"


def test_credentials_are_never_world_readable_even_briefly():
    body = SCRIPT.read_text().split("install_private_file() {")[1].split("\n}\n")[0]
    assert 'install -o "$CROOM_USER" -g "$CROOM_USER" -m 600' in body
    assert "cp " not in body
    for function in ("install_credentials() {", "install_zoom_credentials() {"):
        caller = SCRIPT.read_text().split(function)[1].split("\n}\n")[0]
        assert "install_private_file" in caller


def test_missing_zoom_credentials_file_is_refused_before_install(tmp_path):
    result = run_bash(f"bash {SCRIPT} --zoom-credentials {tmp_path / 'nope.json'}")
    assert result.returncode == 1
    assert "not found" in (result.stdout + result.stderr).lower()


def test_help_mentions_zoom_credentials():
    assert "--zoom-credentials FILE" in run_bash(f"bash {SCRIPT} --help").stdout


def test_zoom_credentials_are_installed_for_the_service_user_only(tmp_path):
    key = tmp_path / "zoom.json"
    key.write_text('{"sdk_client_id": "a", "sdk_client_secret": "b"}')
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={tmp_path / 'etc'}; CROOM_USER=$(id -un); ZOOM_CREDENTIALS_FILE={key}; install_zoom_credentials",
    )
    assert result.returncode == 0, result.stderr
    installed = tmp_path / "etc" / "zoom-credentials.json"
    assert installed.read_text() == key.read_text()
    assert oct(installed.stat().st_mode & 0o777) == "0o600"


def test_reinstalling_with_the_installed_zoom_credentials_keeps_them(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    installed = etc / "zoom-credentials.json"
    installed.write_text('{"sdk_client_id": "a", "sdk_client_secret": "b"}')
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={etc}; CROOM_USER=$(id -un); ZOOM_CREDENTIALS_FILE={installed}; install_zoom_credentials",
    )
    assert result.returncode == 0, result.stderr
    assert installed.read_text() == '{"sdk_client_id": "a", "sdk_client_secret": "b"}'


def test_completion_message_names_the_zoom_check_when_zoom_credentials_were_installed():
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG=/tmp/room.yaml; ZOOM_CREDENTIALS_FILE=/tmp/zoom.json; print_completion")
    assert result.returncode == 0, result.stderr
    assert "croom --check-zoom -c /etc/croom/config.yaml" in result.stdout
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG=/tmp/room.yaml; print_completion")
    assert "--check-zoom" not in result.stdout


def test_profile_folder_is_private_to_the_service_user(tmp_path):
    result = run_bash(
        f"source {SCRIPT}; INSTALL_DIR={tmp_path / 'opt'}; CONFIG_DIR={tmp_path / 'etc'}; DATA_DIR={tmp_path / 'lib'}; "
        f"LOG_DIR={tmp_path / 'log'}; CROOM_USER=$(id -un); create_directories"
    )
    assert result.returncode == 0, result.stderr
    profile = tmp_path / "lib" / "meet-profile"
    assert profile.is_dir() and oct(profile.stat().st_mode & 0o777) == "0o700"


def test_completion_names_the_meet_sign_in_when_the_room_config_has_a_profile(tmp_path):
    room = tmp_path / "room.yaml"
    room.write_text("meeting:\n  google_profile_dir: /var/lib/croom/meet-profile\n")
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG={room}; CROOM_USER=pi; print_completion")
    assert result.returncode == 0, result.stderr
    assert "sudo -u pi DISPLAY=:0 /opt/croom/venv/bin/croom --sign-in-meet -c /etc/croom/config.yaml" in result.stdout
    assert "sudo systemctl stop croom" in result.stdout
    plain = tmp_path / "plain.yaml"
    plain.write_text("meeting:\n  platforms: [zoom]\n")
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG={plain}; CROOM_USER=pi; print_completion")
    assert "--sign-in-meet" not in result.stdout


def test_desktop_launcher_starts_the_service_through_a_limited_sudoers_rule(tmp_path):
    """A 'Start Crystal Meet' icon on the TV Pi's desktop (over VNC) restarts the service; the
    desktop user gets sudo for exactly that and nothing else."""
    desktop, sudoers, install = tmp_path / "Desktop", tmp_path / "sudoers.d", tmp_path / "opt"
    icon = REPO / "src" / "croom" / "control" / "static" / "crystal-meet.svg"
    result = run_bash(f"source {SCRIPT}; CROOM_USER=$(id -un); DESKTOP_DIR={desktop}; SUDOERS_DIR={sudoers}; "
                      f"INSTALL_DIR={install}; ICON_SOURCE={icon}; install_desktop_launcher")
    assert result.returncode == 0, result.stderr
    me = subprocess.check_output(["id", "-un"], text=True).strip()
    entry = (desktop / "crystal-meet.desktop").read_text()
    assert "[Desktop Entry]" in entry and "Name=Start Crystal Meet" in entry and "Type=Application" in entry
    assert "Exec=/usr/bin/sudo -n /usr/bin/systemctl restart croom" in entry
    assert "Terminal=false" in entry and f"Icon={install}/share/crystal-meet.svg" in entry
    assert os.access(desktop / "crystal-meet.desktop", os.X_OK)
    assert (install / "share" / "crystal-meet.svg").read_text().lstrip().startswith("<svg")
    rule = (sudoers / "croom").read_text()
    assert rule.strip() == f"{me} ALL=(root) NOPASSWD: /usr/bin/systemctl start croom, /usr/bin/systemctl restart croom"
    assert oct((sudoers / "croom").stat().st_mode & 0o777) == "0o440"


def test_completion_message_names_the_desktop_icon():
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG=room.yaml; print_completion")
    assert "Start Crystal Meet" in result.stdout


def test_installer_adds_the_pulse_client_library_for_the_browsers_audio():
    """Chromium reaches PipeWire through libpulse; a fresh Pi may not have it."""
    body = SCRIPT.read_text().split("install_dependencies() {", 1)[1].split("\n}", 1)[0]
    assert "libpulse0" in body


def launcher_body():
    return SCRIPT.read_text().split("install_desktop_launcher() {", 1)[1].split("\n}\n", 1)[0]


def test_the_sudoers_rule_is_validated_before_it_is_moved_into_place():
    """sudo reads every file in /etc/sudoers.d: a rule written there and checked afterwards is live, broken or not,
    until the check removes it. The rule goes to a temporary file beside it (a name with a dot, which sudo skips),
    is checked there, and only a rule that passed is moved into place, already at mode 440."""
    body = launcher_body()
    assert '> "$sudoers_dir/croom"' not in body                       # never written where sudo reads it
    write = body.index('> "$rule_tmp"')
    check = body.index('sudoers_rule_valid "$rule_tmp"')
    mode = body.index('chmod 440 "$rule_tmp"')
    move = body.index('mv -f "$rule_tmp" "$sudoers_dir/croom"')
    assert write < check < move and mode < move
    assert 'mktemp "$sudoers_dir/.croom.' in body
    check_function = SCRIPT.read_text().split("sudoers_rule_valid() {", 1)[1].split("\n}\n", 1)[0]
    assert 'visudo -c -f "$1"' in check_function and "$EUID -eq 0" in check_function


def test_a_sudoers_rule_that_does_not_validate_is_removed_with_a_warning(tmp_path):
    desktop, sudoers, install = tmp_path / "Desktop", tmp_path / "sudoers.d", tmp_path / "opt"
    result = run_bash(f"source {SCRIPT}; sudoers_rule_valid() {{ return 1; }}; CROOM_USER=$(id -un); DESKTOP_DIR={desktop}; "
                      f"SUDOERS_DIR={sudoers}; INSTALL_DIR={install}; ICON_SOURCE=/nonexistent; install_desktop_launcher")
    assert result.returncode == 0, result.stderr                       # a warning, not the end of the installation
    assert [line for line in result.stdout.splitlines() if "[Warning]" in line and "did not validate" in line]
    assert list(sudoers.iterdir()) == []                               # nothing left where sudo would read it
    assert (desktop / "crystal-meet.desktop").exists()


def test_a_sudoers_rule_that_validates_leaves_only_the_rule_behind(tmp_path):
    desktop, sudoers, install = tmp_path / "Desktop", tmp_path / "sudoers.d", tmp_path / "opt"
    result = run_bash(f"source {SCRIPT}; CROOM_USER=$(id -un); DESKTOP_DIR={desktop}; SUDOERS_DIR={sudoers}; "
                      f"INSTALL_DIR={install}; ICON_SOURCE=/nonexistent; install_desktop_launcher")
    assert result.returncode == 0, result.stderr
    assert [path.name for path in sudoers.iterdir()] == ["croom"]      # the temporary file was moved, not copied
    assert oct((sudoers / "croom").stat().st_mode & 0o777) == "0o440"
