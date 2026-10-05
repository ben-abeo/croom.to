#!/bin/bash
#
# Crystal Meet dashboard installer
#
# Runs the dashboard on a Raspberry Pi (or any 64-bit Debian-family machine)
# under Docker Compose. Run it once to install and again to update:
#   sudo bash installer/install-dashboard.sh --admin-email you@example.com
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

CROOM_REPO="${CROOM_REPO:-https://github.com/ben-abeo/croom.to.git}"
CROOM_BRANCH="${CROOM_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/croom-dashboard}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/croom-dashboard}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
MEMORY_KB="${MEMORY_KB:-$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)}"
ADMIN_EMAIL=""
ADDRESS=""
FIRST_RUN="no"

log() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1" >&2; exit 1; }

deploy_dir() { echo "$INSTALL_DIR/deploy/dashboard"; }
env_file() { echo "$(deploy_dir)/.env"; }
compose() { docker compose -f "$(deploy_dir)/docker-compose.yml" "$@"; }

check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "Run with sudo: sudo bash $0 --admin-email you@example.com"
    fi
}

check_platform() {
    local arch
    arch="$(uname -m)"
    case "$arch" in
        aarch64|x86_64) ;;
        *) error "This needs a 64-bit system (found $arch). Flash Raspberry Pi OS Lite 64-bit." ;;
    esac
    if ! command -v apt-get >/dev/null 2>&1; then
        error "This installer needs a Debian-based system with apt (Raspberry Pi OS, Debian, Ubuntu)."
    fi
    if (( MEMORY_KB < 2000000 )); then
        error "At least 2 GB of memory is needed to build the dashboard image (found $((MEMORY_KB / 1024)) MB)."
    fi
    if (( MEMORY_KB < 3800000 )); then
        warn "Less than 4 GB of memory: building the image takes longer."
    fi
}

install_docker() {
    if docker compose version >/dev/null 2>&1; then
        log "Docker Compose is already installed"
        return
    fi
    log "Installing Docker Engine and the compose plugin from download.docker.com"
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl git
    install -m 0755 -d /etc/apt/keyrings
    # shellcheck disable=SC1091
    . /etc/os-release
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        usermod -aG docker "$SUDO_USER"
        log "Added $SUDO_USER to the docker group (takes effect at the next login)"
    fi
}

fetch_source() {
    if ! command -v git >/dev/null 2>&1; then
        apt-get update -qq
        apt-get install -y -qq git
    fi
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        log "Updating $INSTALL_DIR from $CROOM_BRANCH"
        git -C "$INSTALL_DIR" fetch --quiet origin
        git -C "$INSTALL_DIR" checkout --quiet "$CROOM_BRANCH"
        git -C "$INSTALL_DIR" pull --quiet --ff-only origin "$CROOM_BRANCH"
    else
        log "Cloning $CROOM_REPO ($CROOM_BRANCH) into $INSTALL_DIR"
        git clone --quiet --branch "$CROOM_BRANCH" "$CROOM_REPO" "$INSTALL_DIR"
    fi
}

random_hex() { openssl rand -hex "$1"; }

write_env() {
    local file
    file="$(env_file)"
    if [[ -f "$file" ]]; then
        log "Keeping the existing settings in $file"
        if [[ -n "$ADMIN_EMAIL" ]]; then
            warn "--admin-email is ignored: the first admin is already recorded in $file"
        fi
        return
    fi
    if [[ -z "$ADMIN_EMAIL" ]]; then
        error "First install: pass --admin-email you@example.com (the first dashboard admin)"
    fi
    if [[ -z "$ADDRESS" ]]; then
        ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    if [[ -z "$ADDRESS" ]]; then
        error "Could not find this machine's address; pass --address"
    fi
    FIRST_RUN="yes"
    mkdir -p "$(dirname "$file")"
    (
        umask 077
        cat > "$file" <<ENVEOF
# Crystal Meet dashboard settings, written by install-dashboard.sh. Keep private.
DB_NAME=croom
DB_USER=croom
DB_PASSWORD=$(random_hex 16)
JWT_SECRET=$(random_hex 32)
ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_PASSWORD=$(random_hex 8)
BASE_URL=http://$ADDRESS:3001
WS_URL=ws://$ADDRESS:3001
LOG_LEVEL=info
ENVEOF
    )
    chmod 600 "$file"
    if [[ $EUID -eq 0 ]]; then
        chown root:root "$file"
    fi
    log "Wrote $file"
}

start_stack() {
    log "Building and starting the dashboard (the first build takes about ten minutes on a Pi)"
    compose up -d --build
    local i
    for i in $(seq 1 90); do
        if curl -fs http://127.0.0.1:3001/health >/dev/null 2>&1; then
            log "The dashboard is answering"
            return
        fi
        sleep 2
    done
    compose logs --tail 40 dashboard || true
    error "The dashboard did not answer on http://127.0.0.1:3001/health within three minutes"
}

write_backup_units() {
    install -m 755 "$(deploy_dir)/backup.sh" "$BIN_DIR/croom-dashboard-backup"
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    sed -e "s|__BIN_DIR__|$BIN_DIR|g" -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" -e "s|__BACKUP_DIR__|$BACKUP_DIR|g" \
        "$(deploy_dir)/croom-dashboard-backup.service" > "$SYSTEMD_DIR/croom-dashboard-backup.service"
    cp "$(deploy_dir)/croom-dashboard-backup.timer" "$SYSTEMD_DIR/croom-dashboard-backup.timer"
}

enable_backup_timer() {
    systemctl daemon-reload
    systemctl enable --now croom-dashboard-backup.timer
    log "Nightly backups at 02:30 into $BACKUP_DIR"
}

print_completion() {
    local file host admin_email admin_password address
    file="$(env_file)"
    host="$(hostname 2>/dev/null || echo crystal-meet)"
    admin_email="$(grep '^ADMIN_EMAIL=' "$file" | cut -d= -f2-)"
    admin_password="$(grep '^ADMIN_PASSWORD=' "$file" | cut -d= -f2-)"
    address="$(grep '^BASE_URL=' "$file" | sed -e 's|^BASE_URL=http://||' -e 's|:3001$||')"
    echo ""
    echo -e "${GREEN}The Crystal Meet dashboard is running.${NC}"
    echo ""
    echo "Open it:     http://$address  or  http://$address:3001  or  http://$host.local"
    echo "Rooms use:   http://$address:3001   (dashboard.url in each room config)"
    echo "Sign in as:  $admin_email"
    if [[ "$FIRST_RUN" == "yes" ]]; then
        echo "Password:    $admin_password   (shown once; change it on the Settings page)"
    else
        echo "Password:    unchanged (the first one is in $file if it was never changed)"
    fi
    echo ""
    echo "Next: give this machine a fixed address in your router (UniFi: Client Devices > Fixed IP),"
    echo "sign in, change the password, then create one token per room on Provisioning."
    echo ""
    echo "Backups:  $BACKUP_DIR (nightly at 02:30, 14 days kept)"
    echo "Logs:     cd $(deploy_dir) && docker compose logs -f"
    echo "Update:   sudo bash $INSTALL_DIR/installer/install-dashboard.sh"
    echo ""
}

main() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}  Crystal Meet dashboard installer${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""

    check_root
    check_platform
    install_docker
    fetch_source
    write_env
    start_stack
    write_backup_units
    enable_backup_timer
    print_completion
}

# Parse arguments and run only when executed, not when sourced by tests
run_installer() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --admin-email)
                ADMIN_EMAIL="${2:-}"
                if [[ "$ADMIN_EMAIL" != *@*.* ]]; then
                    error "--admin-email needs an email address (got '${ADMIN_EMAIL:-nothing}')"
                fi
                shift 2
                ;;
            --address)
                ADDRESS="${2:-}"
                if [[ -z "$ADDRESS" ]]; then
                    error "--address needs a host name or IP address"
                fi
                shift 2
                ;;
            --help)
                echo "Usage: sudo bash $0 --admin-email EMAIL [--address HOST]"
                echo ""
                echo "Options:"
                echo "  --admin-email EMAIL  The first dashboard admin (required on the first install)"
                echo "  --address HOST       This machine's address as the rooms will see it (default: its first IPv4)"
                echo "  --help               Show this help"
                echo ""
                echo "Environment:"
                echo "  CROOM_REPO    git source to install (default: this fork on GitHub)"
                echo "  CROOM_BRANCH  branch to install (default: main)"
                echo "  INSTALL_DIR   where the clone lives (default: /opt/croom-dashboard)"
                echo "  BACKUP_DIR    where nightly dumps go (default: /var/backups/croom-dashboard)"
                exit 0
                ;;
            *)
                error "Unknown option: $1"
                ;;
        esac
    done
    main
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    run_installer "$@"
fi
