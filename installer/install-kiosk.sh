#!/bin/bash
#
# Crystal Meet table screen (kiosk) installer for Raspberry Pi OS.
#
# Opens the room page full screen in Chromium's kiosk mode on a Raspberry Pi with a
# touch display: at every login, and from a "Room controls" icon on the desktop if
# the browser was ever closed. The page is opened with ?keyboard=1 so it shows its own
# on-screen keyboard, because the Pi's keyboard is hidden under a kiosk window.
#
# It also installs the touch Pi's agent (tools/kiosk_agent), which checks in with the room's
# TV Pi so that Settings there can update and reboot this Pi: a root systemd service run
# from a root-owned copy of this checkout's commit in /opt/croom-kiosk/releases. The Pi
# waits to join until Allow is tapped in Settings > Software on the room page.
#
# Usage: sudo bash installer/install-kiosk.sh --url http://<room device>:8080/
#
set -e

KIOSK_URL="${KIOSK_URL:-}"
KIOSK_USER="${KIOSK_USER:-${SUDO_USER:-}}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
SKIP_PACKAGES="${SKIP_PACKAGES:-}"
SKIP_RASPI_CONFIG="${SKIP_RASPI_CONFIG:-}"
# The agent (room settings spec 2026-10-29, section 7)
SKIP_AGENT="${SKIP_AGENT:-}"
KIOSK_FROM_AGENT="${KIOSK_FROM_AGENT:-}"      # set by the agent when it runs this from a new release: the agent stays as it is
KIOSK_SOURCE="${KIOSK_SOURCE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
KIOSK_REPO_URL="${KIOSK_REPO_URL:-https://github.com/ben-abeo/croom.to.git}"
AGENT_DIR="${AGENT_DIR:-/opt/croom-kiosk}"
AGENT_STATE="${AGENT_STATE:-/var/lib/croom-kiosk}"
AGENT_PYTHON="${AGENT_PYTHON:-/usr/bin/python3}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
RELEASE_OWNER="${RELEASE_OWNER:-root:root}"
AGENT_UNIT="croom-kiosk-agent.service"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[kiosk]${NC} $1"; }
warn() { echo -e "${YELLOW}[kiosk]${NC} $1"; }
error() { echo -e "${RED}[kiosk]${NC} $1" >&2; exit 1; }

kiosk_home() {
    if [[ -n "${KIOSK_HOME:-}" ]]; then echo "$KIOSK_HOME"; else getent passwd "$KIOSK_USER" | cut -d: -f6; fi
}

check_root() {
    [[ $EUID -eq 0 ]] || error "Run with sudo: sudo bash $0 --url http://<room device>:8080/"
}

check_user() {
    if [[ -z "$KIOSK_USER" || "$KIOSK_USER" == "root" ]]; then
        error "Run with sudo from the desktop user's login, so the kiosk starts in that desktop"
    fi
    id "$KIOSK_USER" >/dev/null 2>&1 || error "User not found: $KIOSK_USER"
}

# The room page address with the keyboard flag, whether or not it already has a query.
page_url() {
    if [[ "$KIOSK_URL" == *\?* ]]; then echo "${KIOSK_URL}&keyboard=1"; else echo "${KIOSK_URL}?keyboard=1"; fi
}

install_packages() {
    [[ -n "$SKIP_PACKAGES" ]] && return 0
    if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
        log "Installing Chromium..."
        apt-get update -qq
        apt-get install -y -qq chromium curl
    fi
    command -v curl >/dev/null 2>&1 || apt-get install -y -qq curl
}

# The command both the login autostart and the desktop icon run.
write_kiosk_command() {
    local url
    url=$(page_url)
    mkdir -p "$BIN_DIR"
    cat > "$BIN_DIR/crystal-meet-kiosk" << KIOSK_COMMAND
#!/bin/bash
# Crystal Meet room controls: the room page full screen in kiosk mode (written by install-kiosk.sh).
URL="$url"
# Already open: nothing to do.
if pgrep -f "chromium.*--kiosk" >/dev/null 2>&1; then exit 0; fi
# Wait for the room device to answer, so the browser never opens on an error page after a boot.
for _ in \$(seq 1 60); do
    curl -fs --max-time 2 "\$URL" >/dev/null 2>&1 && break
    sleep 2
done
BROWSER=\$(command -v chromium || command -v chromium-browser)
# 1.5x scale: the 1920x1200 touch panel lays the page out at 1280x800, buttons about 11 mm tall.
exec "\$BROWSER" --kiosk --force-device-scale-factor=1.5 --noerrdialogs --disable-infobars --disable-session-crashed-bubble \\
    --hide-crash-restore-bubble --check-for-update-interval=31536000 --password-store=basic \\
    --overscroll-history-navigation=0 --disable-pinch "\$URL"
KIOSK_COMMAND
    chmod 755 "$BIN_DIR/crystal-meet-kiosk"
    log "Kiosk command written: $BIN_DIR/crystal-meet-kiosk ($url)"
}

# Start the kiosk at every login of the desktop session (labwc, the current Raspberry Pi OS).
write_autostart() {
    local home autostart line
    home=$(kiosk_home)
    autostart="$home/.config/labwc/autostart"
    line="$BIN_DIR/crystal-meet-kiosk &"
    mkdir -p "$(dirname "$autostart")"
    touch "$autostart"
    grep -qxF "$line" "$autostart" || echo "$line" >> "$autostart"
    chown -R "$KIOSK_USER:$KIOSK_USER" "$home/.config/labwc" 2>/dev/null || true
    log "Autostart entry in place: $autostart"
}

# A "Room controls" icon on the desktop reopens the kiosk if it was closed.
write_desktop_icon() {
    local home icon_dir entry
    home=$(kiosk_home)
    icon_dir="$home/.local/share/icons"
    entry="$home/Desktop/room-controls.desktop"
    mkdir -p "$icon_dir" "$home/Desktop"
    cat > "$icon_dir/crystal-meet.svg" << 'ICON_SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#001636"/><stop offset="1" stop-color="#16244F"/></linearGradient></defs>
  <rect width="256" height="256" rx="56" fill="url(#g)"/>
  <svg x="26" y="26" width="204" height="204" viewBox="0 0 560 541.43" fill="#ffffff">
    <path d="M646.06,438.91c-2.09-1.79-4.51-3.13-7.26-4.03-2.75-.89-5.72-1.34-8.9-1.34h-30.74v64.56h9.55v-22.28h21.19c3.18,0,6.15-.43,8.9-1.29,2.75-.86,5.17-2.17,7.26-3.93,2.09-1.75,3.71-3.96,4.88-6.61,1.16-2.65,1.74-5.7,1.74-9.15s-.58-6.6-1.74-9.25c-1.16-2.65-2.79-4.87-4.88-6.67ZM639.59,463.98c-2.36,2.32-5.46,3.48-9.3,3.48h-21.59v-25.27h21.59c3.84,0,6.94,1.13,9.3,3.38,2.36,2.26,3.53,5.34,3.53,9.25s-1.18,6.83-3.53,9.15Z"/>
    <path d="M499.22,287.5l-72.8-124.09c-1.77-2.99-4.75-5.02-8.18-5.57-3.45-.53-6.9.48-9.48,2.78l-107.89,95.7-39.72-150.1c-.85-3.21-3.04-5.9-6.02-7.39l-114.22-57.11c-5.51-2.78-12.21-.73-15.25,4.65l-61.67,109.11c-1.91,3.37-2.02,7.44-.29,10.88l87.88,175.77h-21.92c-3.01-.16-6.07,1.48-7.62,4.16l-79.26,137.28c-2.06,3.58-2.06,8.01,0,11.57,2.06,3.57,5.9,5.79,10.02,5.79h334.48c5.84,0,11.08-3.22,13.67-8.4l97.46-191.74.19-.63c.85-1.17,1.47-2.47,1.84-3.89.78-2.99.34-6.11-1.22-8.77ZM345.31,485.32h-55.35l-100.24-289.39,59.78-72.62,95.8,362.01ZM273.43,485.32h-56.67l-73.64-127.56h86.13l44.18,127.56ZM81.42,156.41l55.95-99,103.53,51.76-62.71,76.19-96.77-28.96ZM169.05,342.14l-84.19-168.38,89.98,26.92,49,141.46h-54.79ZM198.71,485.32H59.82l69.44-120.28,69.44,120.28ZM483.69,291.91l-95.69,55.85-75.38-80.96,102.82-91.21,68.25,116.32ZM387.1,485.32h-4.81l11.62-122.92,80.19-46.8-87,169.72ZM378.42,360.39l-11.82,124.92h-5.13l-52.92-199.97,69.86,75.05Z"/>
  </svg>
</svg>
ICON_SVG
    cat > "$entry" << DESKTOP_ENTRY
[Desktop Entry]
Type=Application
Name=Room controls
Comment=Open the Crystal Meet room page full screen
Exec=$BIN_DIR/crystal-meet-kiosk
Icon=$icon_dir/crystal-meet.svg
Terminal=false
Categories=Utility;
DESKTOP_ENTRY
    chmod 755 "$entry"
    chown "$KIOSK_USER:$KIOSK_USER" "$entry" "$icon_dir/crystal-meet.svg" 2>/dev/null || true
    log "Desktop icon in place: Room controls"
}

disable_blanking() {
    [[ -n "$SKIP_RASPI_CONFIG" ]] && return 0
    if command -v raspi-config >/dev/null 2>&1; then
        raspi-config nonint do_blanking 1 || warn "Could not turn off screen blanking; do it in Raspberry Pi Configuration"
        log "Screen blanking turned off"
    fi
}

# --- The touch Pi's agent ---------------------------------------------------------------

# git on this checkout, which belongs to the desktop user while this runs as root.
source_git() {
    git -c safe.directory="$KIOSK_SOURCE" -c safe.directory="$KIOSK_SOURCE/.git" -C "$KIOSK_SOURCE" "$@"
}

# releases/<commit>: the commit's files, root's and read-only to the others, VERSION written last,
# then renamed into place (a folder without VERSION is unfinished, and the agent removes it).
build_agent_release() {
    local commit="$1" release partial tarball
    release="$AGENT_DIR/releases/$commit"
    if [[ -f "$release/VERSION" && "$(cat "$release/VERSION")" == "$commit" ]]; then
        chown -R "$RELEASE_OWNER" "$release"
        chmod -R go-w,a+rX "$release"
        return 0
    fi
    partial="$AGENT_DIR/releases/.partial-$commit"
    tarball="$AGENT_DIR/.$commit.tar"
    rm -rf "$partial"
    mkdir -p "$partial"
    source_git archive --format=tar -o "$tarball" "$commit"
    tar -xf "$tarball" -C "$partial" --no-same-owner --no-same-permissions
    rm -f "$tarball"
    echo "$commit" > "$partial/VERSION"
    chown -R "$RELEASE_OWNER" "$partial"
    chmod -R go-w,a+rX "$partial"
    sync
    rm -rf "$release"
    mv "$partial" "$release"
}

# A link in AGENT_DIR pointed at releases/<commit> in one step.
point_agent_link() {
    local name="$1" commit="$2"
    ln -sfn "releases/$commit" "$AGENT_DIR/.$name-new"
    mv -Tf "$AGENT_DIR/.$name-new" "$AGENT_DIR/$name"
}

# What the agent needs to know: the room page, and the desktop user the kiosk setup is for.
write_agent_config() {
    install -d -m 700 "$AGENT_STATE"
    python3 - "$AGENT_STATE/config.json" "$KIOSK_URL" "$KIOSK_USER" << 'AGENT_CONFIG'
import json, os, sys
path, url, user = sys.argv[1:4]
fd = os.open(path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"url": url, "user": user}, f)
os.replace(path + ".new", path)
AGENT_CONFIG
}

# The agent's unit runs it from the good release (the last one that checked in), as root.
write_agent_unit() {
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/$AGENT_UNIT" << AGENT_UNIT_FILE
[Unit]
Description=Crystal Meet table screen agent: checks in with the room's TV Pi for updates and reboots
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$AGENT_PYTHON $AGENT_DIR/good/tools/kiosk_agent/croom_kiosk_agent.py --base $AGENT_DIR --state $AGENT_STATE
Restart=always
RestartSec=5
UMask=0022

[Install]
WantedBy=multi-user.target
AGENT_UNIT_FILE
    chmod 644 "$UNIT_DIR/$AGENT_UNIT"
}

install_agent() {
    [[ -n "$SKIP_AGENT" ]] && return 0
    if [[ -n "$KIOSK_FROM_AGENT" ]]; then
        # Run by the agent from a new release: its unit is brought up to date, and the agent is not restarted (it reboots next)
        write_agent_unit
        systemctl daemon-reload || warn "systemctl daemon-reload failed"
        systemctl enable "$AGENT_UNIT" >/dev/null 2>&1 || warn "Could not enable $AGENT_UNIT"
        return 0
    fi
    if [[ -z "$SKIP_PACKAGES" ]] && ! command -v git >/dev/null 2>&1; then
        apt-get install -y -qq git
    fi
    local commit
    commit=$(source_git rev-parse HEAD 2>/dev/null) || error "Run this from a clone of the repository (git clone https://github.com/ben-abeo/croom.to.git)"
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || error "Could not tell which version this clone is: $commit"
    log "Installing the touch Pi agent (version ${commit:0:7})..."
    install -d -m 755 "$AGENT_DIR" "$AGENT_DIR/releases"
    chown "$RELEASE_OWNER" "$AGENT_DIR" "$AGENT_DIR/releases"
    if [[ ! -d "$AGENT_DIR/repo.git" ]]; then
        git -c safe.directory="$KIOSK_SOURCE" -c safe.directory="$KIOSK_SOURCE/.git" clone --quiet --bare "$KIOSK_SOURCE" "$AGENT_DIR/repo.git"
    fi
    git -C "$AGENT_DIR/repo.git" remote set-url origin "$KIOSK_REPO_URL"
    chown -R "$RELEASE_OWNER" "$AGENT_DIR/repo.git"
    build_agent_release "$commit"
    point_agent_link current "$commit"
    point_agent_link good "$commit"
    write_agent_config
    rm -f "$AGENT_STATE/update.json" "$AGENT_STATE/trial-ok"   # this run is the version now: no update of before is under way
    write_agent_unit
    systemctl daemon-reload
    systemctl enable "$AGENT_UNIT" >/dev/null 2>&1
    systemctl restart "$AGENT_UNIT"
    log "Agent running: open Settings > Software on the room page and tap Allow under Devices waiting to join"
}

print_completion() {
    echo ""
    echo -e "${GREEN}Crystal Meet table screen set up.${NC}"
    echo "Room page: $(page_url)"
    echo "It opens full screen at every login; the Room controls icon on the desktop reopens it."
    echo "Open it now:  sudo -u $KIOSK_USER $BIN_DIR/crystal-meet-kiosk &"
    echo "To change the address later, run this installer again with the new --url."
    if [[ -z "$SKIP_AGENT" && -z "$KIOSK_FROM_AGENT" ]]; then
        echo "This Pi now asks to join the room: on the room page open Options > Settings > Software and tap Allow"
        echo "under Devices waiting to join. From then on Settings updates and reboots it."
    fi
    echo ""
}

main() {
    check_root
    check_user
    install_packages
    write_kiosk_command
    write_autostart
    write_desktop_icon
    disable_blanking
    install_agent
    print_completion
}

run_installer() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --url)
                KIOSK_URL="${2:-}"
                [[ -n "$KIOSK_URL" ]] || error "--url needs the room page address, for example --url http://pimeet-3.local:8080/"
                shift 2
                ;;
            --help)
                echo "Usage: sudo bash $0 --url http://<room device>:8080/"
                echo ""
                echo "Sets up this Raspberry Pi as the room's table screen: the room page opens full"
                echo "screen in Chromium's kiosk mode at every login, with its own on-screen keyboard,"
                echo "and a Room controls icon on the desktop reopens it if it is closed."
                echo ""
                echo "Options:"
                echo "  --url ADDRESS   The room page, http://<room device address>:8080/ (required)"
                exit 0
                ;;
            *)
                error "Unknown option: $1 (see --help)"
                ;;
        esac
    done
    [[ -n "$KIOSK_URL" ]] || error "Say which room page to show: --url http://<room device>:8080/"
    main
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    run_installer "$@"
fi
