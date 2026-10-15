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
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
# The agent runs as the desktop user that invoked sudo, so Chromium can use the
# screen and audio of the signed-in session. Override the source with CROOM_REPO.
CROOM_USER="${SUDO_USER:-}"
CROOM_REPO="${CROOM_REPO:-git+https://github.com/ben-abeo/croom.to.git}"
ROOM_CONFIG=""
CREDENTIALS_FILE=""
ZOOM_CREDENTIALS_FILE=""
# Where the touchpad's virtual mouse is given to the input group, and where the module that makes it is set to load at boot
UDEV_RULES_DIR="${UDEV_RULES_DIR:-/etc/udev/rules.d}"
MODULES_LOAD_DIR="${MODULES_LOAD_DIR:-/etc/modules-load.d}"

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

    # Core dependencies (python3-dev, gcc and libc6-dev: evdev, which makes the room Pi's virtual mouse, has no wheel: pip compiles it, and
    # its build reads the kernel's headers, which libc6-dev brings and gcc only recommends)
    apt-get install -y \
        python3 \
        python3-pip \
        python3-venv \
        python3-dev \
        gcc \
        libc6-dev \
        "$CHROMIUM_PKG" \
        pulseaudio \
        libpulse0 \
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

# The touchscreen's touchpad can move the room Pi's own mouse: the agent makes a virtual one on /dev/uinput (evdev). The device is the
# input group's to open, and the module that provides it is loaded at every boot.
write_uinput_rules() {
    mkdir -p "$UDEV_RULES_DIR" "$MODULES_LOAD_DIR"
    cat > "$UDEV_RULES_DIR/99-croom-uinput.rules" << 'EOF'
# Crystal Meet: the touchpad's virtual mouse on the room Pi is made on /dev/uinput, which the input group may use
KERNEL=="uinput", GROUP="input", MODE="0660"
EOF
    echo "uinput" > "$MODULES_LOAD_DIR/croom-uinput.conf"
}

# Set the virtual mouse up: the rule and the module at boot (write_uinput_rules), the agent's user in the input group, and the
# module loaded now with the rule in force, so that the touchpad works without a reboot
install_virtual_mouse() {
    log "Setting up the room Pi's virtual mouse..."
    write_uinput_rules
    # create_user adds this group too, but its one usermod fails as a whole, and silently, where any group it names does not exist
    # (a PC has no gpio group)
    usermod -aG input "$CROOM_USER" || warn "Could not add $CROOM_USER to the input group: the touchpad cannot move the room Pi's mouse"
    udevadm control --reload-rules || true
    modprobe uinput || warn "The uinput module could not be loaded now: the touchpad cannot move the room Pi's mouse until it is (it is set to load at boot)"
    udevadm trigger --subsystem-match=misc --sysname-match=uinput || true
}

# Create directory structure
create_directories() {
    log "Creating directories..."

    mkdir -p "$INSTALL_DIR"
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$DATA_DIR"
    mkdir -p "$LOG_DIR"
    mkdir -p "$INSTALL_DIR/models"
    # The room's signed-in Google Meet profile: the service user's alone (spec 2026-10-07, section 4.6)
    mkdir -p "$DATA_DIR/meet-profile"
    chmod 700 "$DATA_DIR/meet-profile"

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

    # Install the browser where the service user can find it: under sudo the
    # default would be root's ~/.cache, invisible to the desktop user at runtime.
    export PLAYWRIGHT_BROWSERS_PATH="$INSTALL_DIR/browsers"
    "$INSTALL_DIR/venv/bin/pip" install playwright
    "$INSTALL_DIR/venv/bin/playwright" install chromium
    chown -R "$CROOM_USER:$CROOM_USER" "$INSTALL_DIR/browsers"

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
  # The practice's name beside the logo on the door sign; "" shows the logo alone.
  company: "Crystal Practice Management"

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

# Copy a secret file into place for the service user only (mode 600). A reinstall
# may name the already installed file itself; keep it and fix owner and mode.
install_private_file() {
    local source="$1" target="$2" label="$3"
    mkdir -p "$CONFIG_DIR"
    if [[ "$source" -ef "$target" ]]; then
        chown "$CROOM_USER:$CROOM_USER" "$target"
        chmod 600 "$target"
    else
        # install(1) creates the file with its final owner and mode, never world-readable
        install -o "$CROOM_USER" -g "$CROOM_USER" -m 600 "$source" "$target"
    fi
    log "Installed $label at $target"
}

# The Google service account key (calendar spec 4.5)
install_credentials() {
    if [[ -z "$CREDENTIALS_FILE" ]]; then
        return
    fi
    install_private_file "$CREDENTIALS_FILE" "$CONFIG_DIR/google-service-account.json" "Google Calendar credentials"
}

# The Zoom Meeting SDK credentials (Zoom spec 4.7)
install_zoom_credentials() {
    if [[ -z "$ZOOM_CREDENTIALS_FILE" ]]; then
        return
    fi
    install_private_file "$ZOOM_CREDENTIALS_FILE" "$CONFIG_DIR/zoom-credentials.json" "Zoom credentials"
}

# Write the systemd units (separate from create_service so tests can call it)
write_units() {
    mkdir -p "$SYSTEMD_DIR"
    local uid
    uid=$(id -u "$CROOM_USER")

    # The agent opens a headed browser, so it must start after the desktop
    # session exists: order after the display manager and wait for the X
    # display (up to two minutes, then systemd restarts the unit and it waits again).
    cat > "$SYSTEMD_DIR/croom.service" << EOF
[Unit]
Description=Crystal Meet room agent (croom)
After=display-manager.service network-online.target pulseaudio.service
Wants=network-online.target

[Service]
Type=simple
User=$CROOM_USER
Group=$CROOM_USER
WorkingDirectory=$INSTALL_DIR
ExecStartPre=/usr/bin/timeout 120 /bin/sh -c 'until [ -S /tmp/.X11-unix/X0 ]; do sleep 2; done'
ExecStart=$INSTALL_DIR/venv/bin/python -m croom.core.agent -c $CONFIG_DIR/config.yaml
Restart=always
RestartSec=10
Environment=DISPLAY=:0
Environment=XDG_RUNTIME_DIR=/run/user/$uid
Environment=PLAYWRIGHT_BROWSERS_PATH=$INSTALL_DIR/browsers

[Install]
WantedBy=multi-user.target
EOF

    # Touch UI service (optional)
    cat > "$SYSTEMD_DIR/croom-ui.service" << EOF
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
Environment=XDG_RUNTIME_DIR=/run/user/$uid
Environment=PLAYWRIGHT_BROWSERS_PATH=$INSTALL_DIR/browsers
Environment=QT_QPA_PLATFORM=eglfs

[Install]
WantedBy=graphical.target
EOF
}

# Create systemd service
create_service() {
    log "Creating systemd service..."
    write_units
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
    echo "If the service ever stops, the Start Crystal Meet icon on this device's desktop (over VNC) starts it again."
    echo ""
    if [[ -n "$ROOM_CONFIG" ]]; then
        echo "Next steps:"
        echo "1. Start service: sudo systemctl start croom"
        echo "2. Check status: sudo systemctl status croom"
        echo "3. View logs: sudo journalctl -u croom -f"
    else
        echo "Next steps:"
        echo "1. Edit configuration: sudo nano $CONFIG_DIR/config.yaml"
        echo "2. Start service: sudo systemctl start croom"
        echo "3. Check status: sudo systemctl status croom"
        echo "4. View logs: sudo journalctl -u croom -f"
        echo ""
        echo "To connect to the dashboard:"
        echo "1. Create an enrollment token on the dashboard's Provisioning page"
        echo "2. Add to config: dashboard.enrollment_token"
        echo "3. Restart: sudo systemctl restart croom"
    fi
    if [[ -n "$CREDENTIALS_FILE" ]]; then
        echo ""
        echo "Check the calendar: $INSTALL_DIR/venv/bin/croom --check-calendar -c $CONFIG_DIR/config.yaml"
    fi
    if [[ -n "$ZOOM_CREDENTIALS_FILE" ]]; then
        echo ""
        echo "Check Zoom: $INSTALL_DIR/venv/bin/croom --check-zoom -c $CONFIG_DIR/config.yaml"
    fi
    if [[ -n "$ROOM_CONFIG" ]] && grep -q "google_profile_dir" "$ROOM_CONFIG"; then
        echo ""
        echo "Sign the room in to Google Meet once (over VNC from a laptop, or with a keyboard here):"
        echo "  sudo systemctl stop croom"
        echo "  sudo -u $CROOM_USER DISPLAY=:0 $INSTALL_DIR/venv/bin/croom --sign-in-meet -c $CONFIG_DIR/config.yaml"
        echo "  sudo systemctl start croom"
    fi
    echo ""
}

# True when a sudoers fragment parses. Only root runs visudo here; anyone else (the tests, with
# SUDOERS_DIR in a scratch folder) cannot check and gets a pass.
sudoers_rule_valid() {
    if [[ $EUID -eq 0 ]] && command -v visudo >/dev/null 2>&1; then
        visudo -c -f "$1" >/dev/null 2>&1
    fi
}

# Desktop launcher: a "Start Crystal Meet" icon on the desktop (reachable over VNC) restarts
# the service when it has stopped; the desktop user gets sudo for exactly that command.
install_desktop_launcher() {
    local home desktop_dir sudoers_dir share_dir icon_source rule_tmp
    home=$(getent passwd "$CROOM_USER" | cut -d: -f6)
    desktop_dir="${DESKTOP_DIR:-$home/Desktop}"
    sudoers_dir="${SUDOERS_DIR:-/etc/sudoers.d}"
    share_dir="$INSTALL_DIR/share"
    icon_source="${ICON_SOURCE:-}"
    if [[ -z "$icon_source" && -x "$INSTALL_DIR/venv/bin/python" ]]; then
        icon_source=$("$INSTALL_DIR/venv/bin/python" -c "import pathlib, croom.control; print(pathlib.Path(croom.control.__file__).parent / 'static' / 'crystal-meet.svg')" 2>/dev/null || true)
    fi
    mkdir -p "$share_dir" "$desktop_dir" "$sudoers_dir"
    if [[ -n "$icon_source" && -f "$icon_source" ]]; then
        cp "$icon_source" "$share_dir/crystal-meet.svg"
        chown "$CROOM_USER:$CROOM_USER" "$share_dir/crystal-meet.svg" 2>/dev/null || true
    else
        warn "Launcher icon not found; the desktop launcher shows a generic icon"
    fi
    cat > "$desktop_dir/crystal-meet.desktop" << DESKTOP_ENTRY
[Desktop Entry]
Type=Application
Name=Start Crystal Meet
Comment=Start the room's Crystal Meet service if it has stopped
Exec=/usr/bin/sudo -n /usr/bin/systemctl restart croom
Icon=$share_dir/crystal-meet.svg
Terminal=false
Categories=Utility;
DESKTOP_ENTRY
    chmod 755 "$desktop_dir/crystal-meet.desktop"
    chown "$CROOM_USER:$CROOM_USER" "$desktop_dir/crystal-meet.desktop" 2>/dev/null || true
    # sudo reads every file in sudoers.d, so the rule is written beside it under a name with a dot (which
    # sudo skips), checked there, and moved into place only once it has passed, already at mode 440.
    rule_tmp=$(mktemp "$sudoers_dir/.croom.XXXXXX")
    printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl start croom, /usr/bin/systemctl restart croom\n' "$CROOM_USER" > "$rule_tmp"
    chmod 440 "$rule_tmp"
    if ! sudoers_rule_valid "$rule_tmp"; then
        rm -f "$rule_tmp"
        warn "The sudoers rule for the desktop launcher did not validate and was not installed: the Start Crystal Meet icon cannot start the service"
        return 0
    fi
    mv -f "$rule_tmp" "$sudoers_dir/croom"
    log "Desktop launcher installed: Start Crystal Meet"
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
    install_virtual_mouse
    create_directories
    install_dependencies
    install_croom
    create_config
    install_credentials
    install_zoom_credentials
    create_service
    install_desktop_launcher
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
            --credentials)
                CREDENTIALS_FILE="$2"
                if [[ -z "$CREDENTIALS_FILE" || ! -f "$CREDENTIALS_FILE" ]]; then
                    error "Credentials file not found: ${CREDENTIALS_FILE:-<missing>}"
                fi
                shift 2
                ;;
            --zoom-credentials)
                ZOOM_CREDENTIALS_FILE="$2"
                if [[ -z "$ZOOM_CREDENTIALS_FILE" || ! -f "$ZOOM_CREDENTIALS_FILE" ]]; then
                    error "Zoom credentials file not found: ${ZOOM_CREDENTIALS_FILE:-<missing>}"
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
                echo "  --credentials FILE  Install a Google service account key as /etc/croom/google-service-account.json"
                echo "  --zoom-credentials FILE  Install Zoom Meeting SDK credentials as /etc/croom/zoom-credentials.json"
                echo "  --enable-ui     Enable Touch UI service"
                echo "  --no-service    Don't create systemd services"
                echo "  --help          Show this help"
                echo ""
                echo "Environment:"
                echo "  CROOM_REPO      pip source to install (default: this fork on GitHub)"
                echo ""
                echo "Every install also sets up the room Pi's virtual mouse for the table's touchpad (it needs python3-dev, gcc and libc6-dev, which the install adds):"
                echo "  a udev rule that gives /dev/uinput to the input group, the uinput module at boot, and the desktop user in the input group."
                echo "  To redo only that step on a device that is already installed: sudo bash -c 'source installer/install.sh && install_virtual_mouse'"
                echo ""
                echo "Checks to run afterwards, as the desktop user the service runs as:"
                echo "  $INSTALL_DIR/venv/bin/croom --check-calendar -c $CONFIG_DIR/config.yaml   the room's Google Calendar"
                echo "  $INSTALL_DIR/venv/bin/croom --check-zoom -c $CONFIG_DIR/config.yaml       the Zoom credentials"
                echo "  $INSTALL_DIR/venv/bin/croom --check-tv -c $CONFIG_DIR/config.yaml         the room's LG TV (with none set up, the LG TVs on the network)"
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
