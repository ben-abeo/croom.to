"""
Tests for installer/install-kiosk.sh, the table Pi's one-command setup: the room
page full screen in Chromium's kiosk mode, started at login and from a desktop
icon. Nothing here needs root, apt or a Pi.
"""

import os
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
