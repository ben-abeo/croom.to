"""
Tests for installer/install.sh that do not need root or apt: argument
handling, the desktop-user check, and config creation with the script sourced.
"""

import logging
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from croom.core.config import (
    DEFAULT_ACKNOWLEDGE,
    DEFAULT_REACTIONS,
    Config,
    ControlsConfig,
    DialogsConfig,
)

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


def test_default_config_writes_out_the_dialog_and_control_settings_at_their_defaults(tmp_path, caplog):
    """Spec 2026-10-13, section 8: the stock config carries the settings so an admin can edit them. They are the defaults the agent would
    use without them (Zoom's own reaction names, the notices it acknowledges), and the file loads without a word from the config."""
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={tmp_path}; CROOM_USER=$(id -un); create_config",
    )
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "config.yaml").read_text()
    data = yaml.safe_load(text)
    assert data["meeting"]["dialogs"] == {"acknowledge": DEFAULT_ACKNOWLEDGE, "ask_everything": False}
    assert data["meeting"]["controls"] == {"reactions": DEFAULT_REACTIONS}
    assert "Dialogs Zoom draws over the TV" in text and "ask_everything: true makes them all wait" in text
    caplog.set_level(logging.WARNING, logger="croom.core.config")
    config = Config.from_dict(data)
    assert config.meeting.dialogs == DialogsConfig() and config.meeting.controls == ControlsConfig()
    assert not caplog.records


def test_default_config_names_the_company_for_the_door_sign(tmp_path):
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={tmp_path}; CROOM_USER=$(id -un); create_config",
    )
    assert result.returncode == 0, result.stderr
    room = (tmp_path / "config.yaml").read_text().split("room:")[1].split("\n\n")[0]
    assert 'company: "Crystal Practice Management"' in room


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


def test_the_root_helper_runs_as_root_for_the_service_users_files_and_socket(tmp_path):
    """croom-admin (spec 2026-10-29, section 5): root, so no User= line; the room's user owns what it writes and is the socket's group."""
    result = run_bash(f"source {SCRIPT}; SYSTEMD_DIR={tmp_path}; CROOM_USER=$(id -un); write_units")
    assert result.returncode == 0, result.stderr
    me = subprocess.check_output(["id", "-un"], text=True).strip()
    group = subprocess.check_output(["id", "-gn"], text=True).strip()   # the user's primary group, which need not share its name
    unit = (tmp_path / "croom-admin.service").read_text()
    assert "Description=Crystal Meet root helper (croom-admin)" in unit
    assert "After=network.target" in unit
    assert "User=" not in unit and "Group=" not in unit
    assert f"ExecStart=/opt/croom/venv/bin/python -m croom.admin --owner {me} --group {group}" in unit
    assert "Restart=always" in unit
    assert "WantedBy=multi-user.target" in unit


def test_the_root_helpers_group_is_the_users_primary_group_by_its_own_name(tmp_path):
    """The socket's group is the room user's primary group as `id -gn` names it, not a group assumed to share the user's name."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_id = bin_dir / "id"
    fake_id.write_text('#!/bin/bash\ncase "$1" in -gn) echo staff ;; -u) echo 1000 ;; *) exit 1 ;; esac\n')
    fake_id.chmod(0o755)
    units = tmp_path / "units"
    result = run_bash(f"source {SCRIPT}; SYSTEMD_DIR={units}; CROOM_USER=pi; write_units", env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert result.returncode == 0, result.stderr
    assert "-m croom.admin --owner pi --group staff\n" in (units / "croom-admin.service").read_text()
    # The agent runs in that group too, so that it can always reach the helper's socket
    for unit in ("croom.service", "croom-ui.service"):
        text = (units / unit).read_text()
        assert "User=pi\n" in text and "Group=staff\n" in text, unit


def test_the_root_helper_is_enabled_and_started():
    script = SCRIPT.read_text()
    enable = script[script.index("enable_services() {"):]
    assert "systemctl enable --now croom-admin.service" in enable[:enable.index("\n}")]


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
        f"LOG_DIR={tmp_path / 'log'}; CROOM_USER=$(id -un); RELEASE_OWNER=$(id -un):$(id -gn); ADMIN_DIR={tmp_path / 'admin'}; create_directories"
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


def test_the_room_pis_virtual_mouse_is_the_input_groups_to_use(tmp_path):
    """The touchpad drives a virtual mouse on the room Pi through /dev/uinput: a udev rule gives the device to the input group, and the
    uinput kernel module is loaded at every boot."""
    rules, modules = tmp_path / "rules.d", tmp_path / "modules-load.d"
    result = run_bash(f"source {SCRIPT}; UDEV_RULES_DIR={rules}; MODULES_LOAD_DIR={modules}; write_uinput_rules")
    assert result.returncode == 0, result.stderr
    (rule_file,) = rules.iterdir()
    assert rule_file.suffix == ".rules"                                    # (udev reads no other file)
    assert 'KERNEL=="uinput", GROUP="input", MODE="0660"' in rule_file.read_text().splitlines()
    (module_file,) = modules.iterdir()
    assert module_file.suffix == ".conf" and module_file.read_text().split() == ["uinput"]


def test_the_rule_and_the_module_go_where_the_system_reads_them():
    text = SCRIPT.read_text()
    assert 'UDEV_RULES_DIR="${UDEV_RULES_DIR:-/etc/udev/rules.d}"' in text
    assert 'MODULES_LOAD_DIR="${MODULES_LOAD_DIR:-/etc/modules-load.d}"' in text


def test_the_agents_user_joins_the_input_group_and_the_module_is_loaded_now_with_the_rule_in_force():
    body = SCRIPT.read_text().split("install_virtual_mouse() {", 1)[1].split("\n}\n", 1)[0]
    assert 'usermod -aG input "$CROOM_USER"' in body
    rule, reload, load, trigger = (body.index(step) for step in ("write_uinput_rules", "udevadm control --reload-rules", "modprobe uinput",
                                                                  "udevadm trigger"))
    assert rule < reload < load < trigger                                  # the device is made under the rule, or has the rule applied to it


def test_the_installer_sets_the_virtual_mouse_up_with_the_rest():
    assert "install_virtual_mouse" in SCRIPT.read_text().split("\nmain() {", 1)[1].split("\n}\n", 1)[0]


def test_the_installer_adds_what_building_evdev_needs():
    """evdev has no wheel: pip compiles its C extension, which needs Python's headers, a compiler and the kernel's linux/input.h, which comes
    with libc6-dev and which gcc only recommends (a device that installs no recommends has the compiler and not the header)."""
    body = SCRIPT.read_text().split("install_dependencies() {", 1)[1].split("\n}", 1)[0]
    assert "python3-dev" in body.split() and "gcc" in body.split() and "libc6-dev" in body.split()


def test_help_names_the_virtual_mouse_step_and_the_checks_including_the_tvs():
    """The help says what the install sets up for the touchpad's virtual mouse and how to redo that step on its own, and lists the checks
    with the other two where `croom --check-tv` is, as the desktop user the service runs as (the TV's key is private to it)."""
    result = run_bash(f"bash {SCRIPT} --help")
    assert result.returncode == 0
    for needle in (
        "Every install also sets up the room Pi's virtual mouse for the table's touchpad (it needs python3-dev, gcc and libc6-dev, which the install adds):",
        "a udev rule that gives /dev/uinput to the input group, the uinput module at boot, and the desktop user in the input group",
        "To redo only that step on a device that is already installed: sudo bash -c 'source installer/install.sh && install_virtual_mouse'",
        "Checks to run afterwards, as the desktop user the service runs as:",
        "/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml",
        "/opt/croom/venv/bin/croom --check-zoom -c /etc/croom/config.yaml",
        "/opt/croom/venv/bin/croom --check-tv -c /etc/croom/config.yaml",
    ):
        assert needle in result.stdout, needle


def test_the_virtual_mouse_step_can_be_run_on_its_own_from_an_installed_device(tmp_path):
    """What the help and the guides tell a person to type: source the script (it runs nothing when sourced) and call the step. The rule
    and the module land where the folders say, the user joins the group, the module is loaded now: here with stand-ins for the commands
    that need root, so that nothing on this machine changes."""
    bin_dir, rules, modules, calls = tmp_path / "bin", tmp_path / "rules.d", tmp_path / "modules-load.d", tmp_path / "calls.log"
    bin_dir.mkdir()
    for command in ("usermod", "udevadm", "modprobe"):
        stand_in = bin_dir / command
        stand_in.write_text(f'#!/bin/sh\necho "{command} $@" >> "{calls}"\n')
        stand_in.chmod(0o755)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "SUDO_USER": "pi", "UDEV_RULES_DIR": str(rules), "MODULES_LOAD_DIR": str(modules)}
    result = run_bash(f"source {SCRIPT} && install_virtual_mouse", env=env)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["usermod -aG input pi", "udevadm control --reload-rules", "modprobe uinput",
                                              "udevadm trigger --subsystem-match=misc --sysname-match=uinput"]
    (rule_file,) = rules.iterdir()
    assert 'KERNEL=="uinput", GROUP="input", MODE="0660"' in rule_file.read_text().splitlines()
    (module_file,) = modules.iterdir()
    assert module_file.read_text().split() == ["uinput"]


GB_KEYBOARD = '''# KEYBOARD CONFIGURATION FILE

# Consult the keyboard(5) manual page.

XKBMODEL="pc105"
XKBLAYOUT="gb"
XKBVARIANT=""
XKBOPTIONS=""

BACKSPACE="guess"
'''


def test_the_room_pis_keyboard_layout_is_set_to_us_for_typing_on_the_tv(tmp_path):
    """Sign in to Google on the TV types US key codes through the room Pi's virtual keyboard; Raspberry Pi OS starts in GB, where Shift+2
    is " and no address can be typed. The system's layout and labwc's for the desktop user are US; the rest of each file is kept, and a
    second run changes nothing."""
    keyboard, labwc = tmp_path / "keyboard", tmp_path / "home" / ".config" / "labwc"
    keyboard.write_text(GB_KEYBOARD)
    labwc.mkdir(parents=True)
    (labwc / "environment").write_text("XKB_DEFAULT_MODEL=pc105\nXKB_DEFAULT_LAYOUT=gb\nXKB_DEFAULT_VARIANT=extd\n")
    snippet = f"source {SCRIPT}; KEYBOARD_FILE={keyboard}; LABWC_DIR={labwc}; CROOM_USER=$(id -un); set_keyboard_layout"
    result = run_bash(snippet)
    assert result.returncode == 0, result.stderr
    lines = keyboard.read_text().splitlines()
    assert 'XKBLAYOUT="us"' in lines and 'XKBVARIANT=""' in lines and 'XKBLAYOUT="gb"' not in lines
    assert [line for line in lines if line.startswith("XKBLAYOUT=")] == ['XKBLAYOUT="us"']
    assert 'XKBMODEL="pc105"' in lines and 'XKBOPTIONS=""' in lines and 'BACKSPACE="guess"' in lines   # the rest is kept
    environment = (labwc / "environment").read_text().splitlines()
    assert environment == ["XKB_DEFAULT_MODEL=pc105", "XKB_DEFAULT_LAYOUT=us"]          # the GB variant is cleared with the layout
    first = (keyboard.read_text(), (labwc / "environment").read_text())
    assert run_bash(snippet).returncode == 0
    assert (keyboard.read_text(), (labwc / "environment").read_text()) == first                         # idempotent


def test_the_keyboard_layout_files_are_made_when_there_are_none(tmp_path):
    keyboard, labwc = tmp_path / "default" / "keyboard", tmp_path / "labwc"
    result = run_bash(f"source {SCRIPT}; KEYBOARD_FILE={keyboard}; LABWC_DIR={labwc}; CROOM_USER=$(id -un); set_keyboard_layout")
    assert result.returncode == 0, result.stderr
    assert keyboard.read_text().splitlines() == ['XKBLAYOUT="us"', 'XKBVARIANT=""']
    assert (labwc / "environment").read_text().splitlines() == ["XKB_DEFAULT_LAYOUT=us"]


def test_the_keyboard_layout_goes_where_the_system_reads_it_and_is_part_of_every_install():
    text = SCRIPT.read_text()
    assert 'KEYBOARD_FILE="${KEYBOARD_FILE:-/etc/default/keyboard}"' in text
    assert 'labwc_dir="${LABWC_DIR:-$home/.config/labwc}"' in text
    assert "set_keyboard_layout" in text.split("\nmain() {", 1)[1].split("\n}\n", 1)[0]


def test_the_completion_text_says_the_layout_is_us_and_applies_after_a_reboot():
    result = run_bash(f"source {SCRIPT}; hostname() {{ echo room; }}; print_completion")
    assert result.returncode == 0, result.stderr
    assert "The keyboard layout is now US (Sign in to Google on the TV types with it): reboot once for the desktop to use it." in result.stdout


def test_the_installer_keeps_the_config_the_room_starts_with_as_config_yaml_good(tmp_path):
    """The root helper puts config.yaml.good back when the room does not start with a change made in Settings: the installer writes it
    at every install and upgrade, a copy of config.yaml, owner the room user, mode 640."""
    room = tmp_path / "room.yaml"
    room.write_text("version: 2\nroom:\n  name: Room 9\n")
    etc = tmp_path / "etc"
    result = run_bash(f"source {SCRIPT}; CONFIG_DIR={etc}; CROOM_USER=$(id -un); ROOM_CONFIG={room}; create_config; keep_good_config")
    assert result.returncode == 0, result.stderr
    good = etc / "config.yaml.good"
    assert good.read_text() == room.read_text() and oct(good.stat().st_mode & 0o777) == "0o640"
    assert sorted(p.name for p in etc.iterdir()) == ["config.yaml", "config.yaml.good"]
    (etc / "config.yaml").write_text("version: 2\nroom:\n  name: Room 10\n")    # an upgrade: the config as it is now
    assert run_bash(f"source {SCRIPT}; CONFIG_DIR={etc}; CROOM_USER=$(id -un); keep_good_config").returncode == 0
    assert good.read_text() == "version: 2\nroom:\n  name: Room 10\n"
    main = SCRIPT.read_text().split("\nmain() {", 1)[1].split("\n}\n", 1)[0]
    assert main.index("create_config") < main.index("keep_good_config")
