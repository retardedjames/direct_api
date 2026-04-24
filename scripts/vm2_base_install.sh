#!/usr/bin/env bash
# VM-2 (and future VM-N) base install: Waydroid + adb + Python deps + VNC
# Target: Ubuntu 26.04 LTS ARM64 on GCP t2a (kernel 7.x ships binder_linux).
# Safe to re-run — each step is idempotent / skipped if already done.
#
# Run as the login user (with passwordless sudo). Do NOT run under sudo directly;
# the `waydroid init` step is run as root but the waydroid session is per-user.

set -euo pipefail

log() { printf '\n\033[1;34m[vm2_base_install]\033[0m %s\n' "$*"; }

# ---------- 0. sanity ----------
if [ "$(uname -m)" != "aarch64" ]; then
    echo "ERROR: this script is for ARM64 VMs only (got $(uname -m))" >&2
    exit 1
fi
if ! grep -q "Ubuntu" /etc/os-release; then
    echo "ERROR: expected Ubuntu (got $(grep ^NAME /etc/os-release))" >&2
    exit 1
fi

# ---------- 1. apt packages ----------
log "apt update + base packages"
sudo apt-get update -y
sudo apt-get install -y \
    curl wget git ca-certificates gnupg \
    python3-pip python3-venv \
    android-tools-adb \
    xvfb x11vnc openbox weston \
    build-essential pkg-config \
    jq

# ---------- 2. binder kernel module ----------
log "ensure binder_linux is loaded and auto-loaded on boot"
if ! lsmod | grep -q '^binder_linux'; then
    sudo modprobe binder_linux
fi
echo 'binder_linux' | sudo tee /etc/modules-load.d/waydroid.conf >/dev/null

# ---------- 3. waydroid ----------
log "install waydroid from upstream repo"
if ! command -v waydroid >/dev/null; then
    curl -fsSL https://repo.waydro.id | sudo bash
    sudo apt-get install -y waydroid
fi

# ---------- 4. waydroid init ----------
# -s GAPPS gives us Google Play services (TT Lite needs them for some checks).
# -f forces re-download if partially initialized. Arch defaults to host (arm64).
if [ ! -f /var/lib/waydroid/waydroid.cfg ]; then
    log "waydroid init (GAPPS, arm64) — this downloads ~1.5GB"
    sudo waydroid init -s GAPPS -f
else
    log "waydroid already initialized (waydroid.cfg present) — skipping init"
fi

# ---------- 5. enable + start container ----------
log "enable waydroid-container service"
sudo systemctl enable waydroid-container
sudo systemctl start waydroid-container

# Wait for container to be running (not the session — that's user-space).
for i in $(seq 1 30); do
    if sudo waydroid status 2>/dev/null | grep -q 'Container.*RUNNING'; then
        break
    fi
    sleep 2
done
sudo waydroid status

# ---------- 6. python deps ----------
log "install python deps (user-site, PEP 668 override)"
pip3 install --user --break-system-packages psycopg2-binary sqlalchemy frida==16.6.6

# ---------- 7. summary ----------
log "done. next steps:"
cat <<'NEXT'

  1. Start a Waydroid session as your login user (NOT as root):
       bash scripts/vm2_start_session.sh
     This brings up the Android UI and adb-connects to 127.0.0.1:5556.

  2. Randomize the device fingerprint BEFORE installing TT Lite:
       sudo bash scripts/vm2_fingerprint_randomize.sh

  3. Install TT Lite splits + frida-server:
       bash scripts/vm2_install_tt.sh

NEXT
