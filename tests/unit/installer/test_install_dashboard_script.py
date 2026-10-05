"""
Tests for installer/install-dashboard.sh that need neither root nor Docker:
argument handling, the platform checks, the env file, the units and the
completion message, with the script sourced.
"""

import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "installer" / "install-dashboard.sh"


def run_bash(snippet, env=None):
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=merged, cwd=REPO)


def env_values(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#"))


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_help_names_both_options():
    result = run_bash(f"bash {SCRIPT} --help")
    assert result.returncode == 0
    assert "--admin-email EMAIL" in result.stdout and "--address HOST" in result.stdout


def test_admin_email_must_look_like_an_address():
    result = run_bash(f"bash {SCRIPT} --admin-email nope")
    assert result.returncode == 1
    assert "email" in (result.stdout + result.stderr).lower()


def test_unknown_option_is_refused():
    assert run_bash(f"bash {SCRIPT} --bogus").returncode == 1


def test_first_install_without_admin_email_is_refused(tmp_path):
    result = run_bash(f"source {SCRIPT}; INSTALL_DIR={tmp_path}; write_env")
    assert result.returncode == 1
    assert "--admin-email" in result.stdout + result.stderr
    assert not (tmp_path / "deploy" / "dashboard" / ".env").exists()


def test_write_env_generates_distinct_hex_secrets_with_mode_600(tmp_path):
    result = run_bash(f"source {SCRIPT}; INSTALL_DIR={tmp_path}; ADMIN_EMAIL=ben@crystalpm.com; ADDRESS=10.0.0.50; write_env")
    assert result.returncode == 0, result.stderr
    env_file = tmp_path / "deploy" / "dashboard" / ".env"
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    values = env_values(env_file)
    assert values["DB_NAME"] == "croom" and values["DB_USER"] == "croom"
    assert re.fullmatch(r"[0-9a-f]{32}", values["DB_PASSWORD"])
    assert re.fullmatch(r"[0-9a-f]{64}", values["JWT_SECRET"])
    assert re.fullmatch(r"[0-9a-f]{16}", values["ADMIN_PASSWORD"])
    assert len({values["DB_PASSWORD"], values["JWT_SECRET"], values["ADMIN_PASSWORD"]}) == 3
    assert values["ADMIN_EMAIL"] == "ben@crystalpm.com"
    assert values["BASE_URL"] == "http://10.0.0.50:3001" and values["WS_URL"] == "ws://10.0.0.50:3001"
    assert values["LOG_LEVEL"] == "info"


def test_second_run_keeps_the_env_file(tmp_path):
    env_file = tmp_path / "deploy" / "dashboard" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("DB_PASSWORD=keep-me\nADMIN_EMAIL=old@crystalpm.com\n")
    result = run_bash(f"source {SCRIPT}; INSTALL_DIR={tmp_path}; ADMIN_EMAIL=new@crystalpm.com; write_env")
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == "DB_PASSWORD=keep-me\nADMIN_EMAIL=old@crystalpm.com\n"
    assert "Keeping" in result.stdout and "ignored" in result.stdout


def test_platform_refuses_32_bit():
    result = run_bash(f"source {SCRIPT}; uname() {{ echo armv7l; }}; check_platform")
    assert result.returncode == 1
    assert "64-bit" in result.stdout + result.stderr


def test_platform_refuses_less_than_2gb_and_warns_below_4gb():
    result = run_bash(f"source {SCRIPT}; check_platform", env={"MEMORY_KB": "1000000"})
    assert result.returncode == 1 and "2 GB" in result.stdout + result.stderr
    # A 2 GB Pi 4 reports about 1.85 GB in /proc/meminfo: it passes with the warning.
    result = run_bash(f"source {SCRIPT}; check_platform", env={"MEMORY_KB": "1850000"})
    assert result.returncode == 0, result.stderr
    assert "4 GB" in result.stdout
    # A 4 GB Pi 4 reports about 3.9 GB: no warning.
    result = run_bash(f"source {SCRIPT}; check_platform", env={"MEMORY_KB": "3900000"})
    assert result.returncode == 0 and "4 GB" not in result.stdout


def test_update_prunes_the_previous_image_after_the_dashboard_answers():
    start = SCRIPT.read_text().split("start_stack() {")[1].split("\n}\n")[0]
    assert "docker image prune -f" in start
    assert start.index("is answering") < start.index("docker image prune -f")


def test_backup_units_are_written_with_the_real_paths(tmp_path):
    result = run_bash(
        f"source {SCRIPT}; INSTALL_DIR={REPO}; SYSTEMD_DIR={tmp_path / 'units'}; BIN_DIR={tmp_path / 'bin'}; "
        f"BACKUP_DIR={tmp_path / 'backups'}; mkdir -p {tmp_path / 'units'} {tmp_path / 'bin'}; write_backup_units"
    )
    assert result.returncode == 0, result.stderr
    service = (tmp_path / "units" / "croom-dashboard-backup.service").read_text()
    assert f"ExecStart={tmp_path / 'bin'}/croom-dashboard-backup {REPO}/deploy/dashboard {tmp_path / 'backups'}" in service
    assert "__" not in service
    timer = (tmp_path / "units" / "croom-dashboard-backup.timer").read_text()
    assert "OnCalendar=*-*-* 02:30:00" in timer
    script = tmp_path / "bin" / "croom-dashboard-backup"
    assert script.is_file() and os.access(script, os.X_OK)
    assert oct((tmp_path / "backups").stat().st_mode & 0o777) == "0o700"


def test_completion_shows_the_password_only_on_the_first_run(tmp_path):
    env_file = tmp_path / "deploy" / "dashboard" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("ADMIN_EMAIL=ben@crystalpm.com\nADMIN_PASSWORD=abc123\nBASE_URL=http://10.0.0.50:3001\n")
    first = run_bash(f"source {SCRIPT}; INSTALL_DIR={tmp_path}; FIRST_RUN=yes; print_completion")
    assert first.returncode == 0, first.stderr
    assert "http://10.0.0.50:3001" in first.stdout and "http://10.0.0.50" in first.stdout
    assert "ben@crystalpm.com" in first.stdout and "abc123" in first.stdout
    assert "install-dashboard.sh" in first.stdout and "/var/backups/croom-dashboard" in first.stdout
    assert "Fixed IP" in first.stdout and "Provisioning" in first.stdout
    assert "sudo docker compose logs -f" in first.stdout  # the settings file is root's, so compose needs sudo
    later = run_bash(f"source {SCRIPT}; INSTALL_DIR={tmp_path}; print_completion")
    assert "abc123" not in later.stdout and "ben@crystalpm.com" in later.stdout


def test_main_runs_the_steps_in_order():
    body = SCRIPT.read_text().split("main() {")[1].split("\n}\n")[0]
    steps = [line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith(("echo", "#"))]
    assert steps == ["check_root", "check_platform", "install_docker", "fetch_source", "write_env", "start_stack",
                     "write_backup_units", "enable_backup_timer", "print_completion"]


def test_docker_comes_from_dockers_repository_and_the_fork_is_the_default_source():
    text = SCRIPT.read_text()
    assert "download.docker.com/linux/" in text and "docker-compose-plugin" in text
    assert 'CROOM_REPO="${CROOM_REPO:-https://github.com/ben-abeo/croom.to.git}"' in text
    assert "pull --quiet --ff-only origin" in text  # updates never merge, only fast-forward
    assert "/health" in text and "--build" in text
