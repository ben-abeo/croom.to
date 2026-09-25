#!/bin/bash
#
# Croom Installer
#
# Non-destructive installation on existing Raspberry Pi OS
# Supports: Bookworm, Trixie
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
CROOM_VERSION="2.0.0-dev"
INSTALL_DIR="/opt/croom"
CONFIG_DIR="/etc/croom"
DATA_DIR="/var/lib/croom"
LOG_DIR="/var/log/croom"
# The agent runs as the desktop user that invoked sudo, so Chromium can use the
# screen and audio of the signed-in session. Override the source with CROOM_REPO.
CROOM_USER="${SUDO_USER:-}"
CROOM_REPO="${CROOM_REPO:-git+https://github.com/ben-abeo/croom.to.git}"
ROOM_CONFIG=""

# Log function
log() {
    echo -e "${GREEN}[Crystal Meet]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[Warning]${NC} $1"
}

error() {
    echo -e "${RED}[Error]${NC} $1"
    exit 1
}

# Check if running as root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)"
    fi
}

# The service needs a desktop session: refuse a bare root shell.
check_desktop_user() {
    if [[ -z "$CROOM_USER" || "$CROOM_USER" == "root" ]]; then
        error "Run this installer with sudo from the desktop user account, for example: sudo bash installer/install.sh"
    fi
    if ! id "$CROOM_USER" &>/dev/null; then
        error "User '$CROOM_USER' does not exist"
    fi
    log "Crystal Meet will run as user $CROOM_USER"
}

# Check platform compatibility
check_platform() {
    log "Checking platform compatibility..."

    # Check architecture
    ARCH=$(uname -m)
    if [[ "$ARCH" != "aarch64" && "$ARCH" != "x86_64" ]]; then
        error "Unsupported architecture: $ARCH (need aarch64 or x86_64)"
    fi

    # Check OS
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        log "Detected: $PRETTY_NAME"

        case "$VERSION_CODENAME" in
            bookworm|trixie|jammy|noble)
                log "OS version supported"
                ;;
            *)
                warn "OS version '$VERSION_CODENAME' not officially supported"
                ;;
        esac
    else
        warn "Could not detect OS version"
    fi

    # Check if Raspberry Pi
    if [[ -f /proc/device-tree/model ]]; then
        MODEL=$(cat /proc/device-tree/model | tr -d '\0')
        log "Hardware: $MODEL"
    fi
}

# Install system dependencies
install_dependencies() {
    log "Installing system dependencies..."

    apt-get update

    # Chromium package name varies by OS (Bookworm: chromium-browser, Trixie: chromium)
    if apt-cache show chromium-browser &>/dev/null; then
        CHROMIUM_PKG=chromium-browser
    elif apt-cache show chromium &>/dev/null; then
        CHROMIUM_PKG=chromium
    else
        error "Chromium package not found (tried chromium-browser and chromium)"
    fi
    log "Using browser package: $CHROMIUM_PKG"

    # Core dependencies
    apt-get install -y \
        python3 \
        python3-pip \
        python3-venv \
        "$CHROMIUM_PKG" \
        pulseaudio \
        v4l-utils \
        libcamera-apps \
        cec-utils \
        git \
        curl \
        wget

    # Qt dependencies for Touch UI
    apt-get install -y \
        python3-pyside6.qtcore \
        python3-pyside6.qtgui \
        python3-pyside6.qtwidgets \
        python3-pyside6.qtqml \
        python3-pyside6.qtquick \
        qml6-module-qtquick \
        qml6-module-qtquick-controls \
        qml6-module-qtquick-layouts \
        qml6-module-qtquick-window || warn "Some Qt packages not available, Touch UI may not work"

    # Optional: AI acceleration support
    if [[ -d /dev/hailo* ]] || lsusb | grep -q "1a6e:089a\|18d1:9302"; then
        log "AI accelerator detected, installing support libraries..."
        # Hailo support would be installed via separate repo
        # Coral support via google-coral packages
    fi

    log "Dependencies installed"
}

# Give the desktop user access to cameras, audio and input devices
create_user() {
    log "Preparing user $CROOM_USER..."
    usermod -a -G video,audio,input,dialout,gpio "$CROOM_USER" 2>/dev/null || true
}

# Create directory structure
create_directories() {
    log "Creating directories..."

    mkdir -p "$INSTALL_DIR"
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$DATA_DIR"
    mkdir -p "$LOG_DIR"
    mkdir -p "$INSTALL_DIR/models"

    chown -R "$CROOM_USER:$CROOM_USER" "$INSTALL_DIR"
    chown -R "$CROOM_USER:$CROOM_USER" "$DATA_DIR"
    chown -R "$CROOM_USER:$CROOM_USER" "$LOG_DIR"
}

# Install Croom Python package
install_croom() {
    log "Installing Croom..."

    # Create virtual environment
    python3 -m venv "$INSTALL_DIR/venv"

    # Install package from the configured repository
    "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
    log "Installing from $CROOM_REPO"
    "$INSTALL_DIR/venv/bin/pip" install "$CROOM_REPO"

    # Install browser automation
    "$INSTALL_DIR/venv/bin/pip" install playwright
    "$INSTALL_DIR/venv/bin/playwright" install chromium

    log "Croom installed"
}

# Create default configuration
create_config() {
    log "Creating configuration..."
    mkdir -p "$CONFIG_DIR"

    if [[ -n "$ROOM_CONFIG" ]]; then
        cp "$ROOM_CONFIG" "$CONFIG_DIR/config.yaml"
        chown "$CROOM_USER:$CROOM_USER" "$CONFIG_DIR/config.yaml"
        chmod 640 "$CONFIG_DIR/config.yaml"
        log "Installed room configuration from $ROOM_CONFIG"
        return
    fi

    if [[ -f "$CONFIG_DIR/config.yaml" ]]; then
        log "Configuration already exists, skipping"
        return
    fi

    cat > "$CONFIG_DIR/config.yaml" << 'EOF'
# Crystal Meet room configuration (croom)
version: 2

room:
  name: "Conference Room"
  location: ""
  timezone: "UTC"

meeting:
  platforms:
    - zoom
    - google_meet
  default_platform: auto
  join_early_minutes: 1
  auto_leave: true
  camera_default_on: true
  mic_default_on: true

calendar:
  providers:
    - google
    - microsoft
  sync_interval_seconds: 60

ai:
  enabled: false
  backend: auto
  person_detection: true
  noise_reduction: true
  echo_cancellation: true
  auto_framing: true
  occupancy_counting: true
  speaker_detection: false
  hand_raise_detection: false
  privacy_mode: false

audio:
  backend: auto
  input_device: auto
  output_device: auto
  noise_reduction_level: medium
  echo_cancellation: true

video:
  backend: auto
  device: auto
  resolution: 1080p
  framerate: 30

display:
  backend: auto
  power_on_boot: true
  power_off_shutdown: true
  touch_enabled: true

control:
  enabled: true
  host: "0.0.0.0"
  port: 8080

dashboard:
  enabled: true
  url: ""
  enrollment_token: ""
  heartbeat_interval_seconds: 30
  metrics_interval_seconds: 60

updates:
  auto_check: true
  auto_install: false
  check_interval_hours: 24
  channel: stable

security:
  admin_pin: ""
  ssh_enabled: true
  require_encryption: true
EOF

    chown "$CROOM_USER:$CROOM_USER" "$CONFIG_DIR/config.yaml"
    chmod 640 "$CONFIG_DIR/config.yaml"

    log "Configuration created at $CONFIG_DIR/config.yaml"
}

# Create systemd service
create_service() {
    log "Creating systemd service..."

    cat > /etc/systemd/system/croom.service << EOF
[Unit]
Description=Crystal Meet room agent (croom)
After=network-online.target pulseaudio.service
Wants=network-online.target

[Service]
Type=simple
User=$CROOM_USER
Group=$CROOM_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python -m croom.core.agent -c $CONFIG_DIR/config.yaml
Restart=always
RestartSec=10
Environment=DISPLAY=:0
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u "$CROOM_USER")

[Install]
WantedBy=multi-user.target
EOF

    # Touch UI service (optional)
    cat > /etc/systemd/system/croom-ui.service << EOF
[Unit]
Description=Crystal Meet touch UI (croom-ui)
After=croom.service
Wants=croom.service

[Service]
Type=simple
User=$CROOM_USER
Group=$CROOM_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python -m croom_ui.main -c $CONFIG_DIR/config.yaml --fullscreen
Restart=always
RestartSec=10
Environment=DISPLAY=:0
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u "$CROOM_USER")
Environment=QT_QPA_PLATFORM=eglfs

[Install]
WantedBy=graphical.target
EOF

    systemctl daemon-reload
    log "Systemd services created"
}

# Enable and start services
enable_services() {
    log "Enabling services..."

    systemctl enable croom.service

    # Only enable UI service if touch display is detected
    if [[ -e /dev/input/touchscreen* ]] || [[ "$ENABLE_UI" == "yes" ]]; then
        systemctl enable croom-ui.service
        log "Touch UI service enabled"
    fi
}

# Print completion message
print_completion() {
    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  Crystal Meet installation complete${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""
    echo "Version: $CROOM_VERSION"
    echo "Install directory: $INSTALL_DIR"
    echo "Configuration: $CONFIG_DIR/config.yaml"
    echo "Room page: http://$(hostname).local:8080/  (or use this device's IP address)"
    echo ""
    echo "Next steps:"
    echo "1. Edit configuration: sudo nano $CONFIG_DIR/config.yaml"
    echo "2. Start service: sudo systemctl start croom"
    echo "3. Check status: sudo systemctl status croom"
    echo "4. View logs: sudo journalctl -u croom -f"
    echo ""
    echo "To connect to management dashboard:"
    echo "1. Get enrollment token from dashboard"
    echo "2. Add to config: dashboard.enrollment_token"
    echo "3. Restart: sudo systemctl restart croom"
    echo ""
}

# Main installation flow
main() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}  Crystal Meet installer v$CROOM_VERSION${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""

    check_root
    check_desktop_user
    check_platform
    create_user
    create_directories
    install_dependencies
    install_croom
    create_config
    create_service
    enable_services
    print_completion
}

# Parse arguments and run only when executed, not when sourced by tests
run_installer() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --config)
                ROOM_CONFIG="$2"
                if [[ -z "$ROOM_CONFIG" || ! -f "$ROOM_CONFIG" ]]; then
                    error "Config file not found: ${ROOM_CONFIG:-<missing>}"
                fi
                shift 2
                ;;
            --enable-ui)
                ENABLE_UI="yes"
                shift
                ;;
            --no-service)
                NO_SERVICE="yes"
                shift
                ;;
            --help)
                echo "Usage: $0 [options]"
                echo ""
                echo "Options:"
                echo "  --config FILE   Install a prepared room config as /etc/croom/config.yaml"
                echo "  --enable-ui     Enable Touch UI service"
                echo "  --no-service    Don't create systemd services"
                echo "  --help          Show this help"
                echo ""
                echo "Environment:"
                echo "  CROOM_REPO      pip source to install (default: this fork on GitHub)"
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
