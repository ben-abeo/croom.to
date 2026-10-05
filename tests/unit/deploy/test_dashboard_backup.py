"""
The backup script dumps the dashboard database through docker compose, keeps
14 days and never leaves a half-written file; the units run it nightly.
"""

import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "deploy" / "dashboard"
SCRIPT = DEPLOY / "backup.sh"


def fake_docker(tmp_path, body):
    """A docker stand-in on PATH; body is the shell that runs for `docker compose ... exec ...`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text("#!/bin/bash\necho \"$@\" > " + str(tmp_path / "docker-args") + "\n" + body + "\n")
    (bin_dir / "docker").chmod(0o755)
    return bin_dir


def run(tmp_path, bin_dir, *args):
    deploy = tmp_path / "deploy"
    deploy.mkdir(exist_ok=True)
    (deploy / ".env").write_text("DB_NAME=croom\nDB_USER=croom\nDB_PASSWORD=secret\n")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    return subprocess.run(["bash", str(SCRIPT), str(deploy), str(tmp_path / "backups"), *args], capture_output=True, text=True, env=env)


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_backup_writes_a_dated_private_gzip_and_prunes_old_files(tmp_path):
    bin_dir = fake_docker(tmp_path, 'echo "-- PostgreSQL database dump"')
    backups = tmp_path / "backups"
    backups.mkdir()
    old = backups / "croom-dashboard-2000-01-01.sql.gz"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 20 * 86400, time.time() - 20 * 86400))
    recent = backups / "croom-dashboard-2026-10-04.sql.gz"
    recent.write_bytes(b"recent")
    result = run(tmp_path, bin_dir)
    assert result.returncode == 0, result.stderr
    today = time.strftime("%Y-%m-%d")
    out = backups / f"croom-dashboard-{today}.sql.gz"
    assert out.is_file() and oct(out.stat().st_mode & 0o777) == "0o600"
    assert subprocess.run(["gunzip", "-c", str(out)], capture_output=True, text=True).stdout.startswith("-- PostgreSQL")
    assert not old.exists() and recent.exists()
    args = (tmp_path / "docker-args").read_text()
    assert "compose -f" in args and "exec -T db pg_dump --clean --if-exists -U croom croom" in args
    assert oct(backups.stat().st_mode & 0o777) == "0o700"


def test_backup_leaves_no_partial_file_when_the_dump_fails(tmp_path):
    bin_dir = fake_docker(tmp_path, 'echo "half a dump"; exit 1')
    result = run(tmp_path, bin_dir)
    assert result.returncode != 0
    assert list((tmp_path / "backups").glob("*.sql.gz")) == []


def test_units_run_nightly_and_catch_up_after_downtime():
    timer = (DEPLOY / "croom-dashboard-backup.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 02:30:00" in timer and "Persistent=true" in timer and "WantedBy=timers.target" in timer
    service = (DEPLOY / "croom-dashboard-backup.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in service
    assert "ExecStart=__BIN_DIR__/croom-dashboard-backup __INSTALL_DIR__/deploy/dashboard __BACKUP_DIR__" in service
    assert "After=docker.service" in service
