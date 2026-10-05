"""
The dashboard's compose file, image and env example are what the installer and
the fourth Pi run; check their shape here so a typo surfaces before a build.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "deploy" / "dashboard"
ENV_KEYS = ["DB_NAME", "DB_USER", "DB_PASSWORD", "JWT_SECRET", "ADMIN_EMAIL", "ADMIN_PASSWORD", "BASE_URL", "WS_URL", "LOG_LEVEL"]


def compose():
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))


def test_two_services_and_a_named_volume():
    data = compose()
    assert data["name"] == "croom-dashboard"
    assert set(data["services"]) == {"db", "dashboard"}
    assert "pgdata" in data["volumes"]
    assert data["services"]["db"]["volumes"] == ["pgdata:/var/lib/postgresql/data"]


def test_database_is_private_and_both_restart():
    data = compose()
    assert "ports" not in data["services"]["db"]
    for service in data["services"].values():
        assert service["restart"] == "unless-stopped"


def test_dashboard_waits_for_a_healthy_database_and_serves_the_web_app():
    dashboard = compose()["services"]["dashboard"]
    assert dashboard["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert dashboard["environment"]["STATIC_DIR"] == "/app/public"
    assert dashboard["environment"]["DB_HOST"] == "db"
    assert dashboard["environment"]["NODE_ENV"] == "production"
    assert dashboard["env_file"] == ".env"
    assert dashboard["ports"] == ["${DASHBOARD_PORT:-3001}:3001", "${DASHBOARD_HTTP_PORT:-80}:3001"]
    assert dashboard["build"] == {"context": "../..", "dockerfile": "deploy/dashboard/Dockerfile"}
    assert dashboard["logging"]["options"]["max-size"] == "10m"
    assert "healthcheck" in dashboard and "healthcheck" in compose()["services"]["db"]


def test_env_example_has_every_key_and_no_secret():
    lines = [l for l in (DEPLOY / ".env.example").read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    keys = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in lines}
    for key in ENV_KEYS:
        assert key in keys, key
    for key in ("DB_PASSWORD", "JWT_SECRET", "ADMIN_PASSWORD", "ADMIN_EMAIL"):
        assert keys[key].startswith("REPLACE_WITH_"), key
    assert keys["BASE_URL"] == "http://REPLACE_WITH_DASHBOARD_ADDRESS:3001"
    assert keys["WS_URL"] == "ws://REPLACE_WITH_DASHBOARD_ADDRESS:3001"


def test_dockerfile_builds_both_halves_and_runs_as_node():
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert text.count("FROM node:20-bookworm-slim") == 3
    assert "AS frontend-build" in text and "AS backend-build" in text
    assert "vite build" in text and "npm run build" in text and "npm prune --omit=dev" in text
    assert "COPY --from=frontend-build /src/frontend/dist ./public" in text
    assert "USER node" in text and 'CMD ["node", "dist/index.js"]' in text
    assert "python3 make g++" in text  # bcrypt builds from source when no arm64 binary is available


def test_dockerignore_keeps_the_context_small():
    text = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert text[0] == "*"
    assert "!src/croom-dashboard/backend" in text and "!src/croom-dashboard/frontend" in text
    for excluded in ("src/croom-dashboard/backend/node_modules", "src/croom-dashboard/frontend/node_modules",
                     "src/croom-dashboard/backend/dist", "src/croom-dashboard/frontend/dist", "src/croom-dashboard/backend/.env"):
        assert excluded in text, excluded


def test_real_env_file_is_ignored_by_git():
    result = subprocess.run(["git", "check-ignore", "-q", "deploy/dashboard/.env"], cwd=REPO)
    assert result.returncode == 0


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_compose_config_resolves(tmp_path):
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
        pytest.skip("docker compose plugin not installed")
    # A copy in tmp_path with a full .env beside it: the real .env never exists in the repo.
    shutil.copy(DEPLOY / "docker-compose.yml", tmp_path / "docker-compose.yml")
    (tmp_path / ".env").write_text("DB_NAME=croom\nDB_USER=croom\nDB_PASSWORD=x\nJWT_SECRET=y\nADMIN_EMAIL=a@b.c\n"
                                   "ADMIN_PASSWORD=z\nBASE_URL=http://h:3001\nWS_URL=ws://h:3001\nLOG_LEVEL=info\n")
    result = subprocess.run(["docker", "compose", "-f", str(tmp_path / "docker-compose.yml"), "config"],
                            capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "croom-dashboard:local" in result.stdout


def test_operator_notes_use_sudo_and_show_backup_copy_and_restore():
    text = (DEPLOY / "README.md").read_text(encoding="utf-8")
    # .env is root-only, so compose cannot be run by the pi user without sudo.
    for match in re.finditer(r"docker compose", text):
        assert text[max(0, match.start() - 5):match.start()] == "sudo ", text[match.start() - 40:match.end() + 20]
    assert "sudo cp /var/backups/croom-dashboard/" in text and "sudo chown" in text
    assert "sudo gunzip -c /var/backups/croom-dashboard/" in text
    assert "-v ON_ERROR_STOP=1" in text
    assert "sudo systemctl start croom-dashboard-backup.service" in text
