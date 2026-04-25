#!/usr/bin/env bash
# Install TT Lite (split bundle) + frida-server 16.6.6 on VM-2.
#
# Prerequisites:
#   1. Base install done (scripts/vm2_base_install.sh)
#   2. Fingerprint randomized BEFORE this script (scripts/vm2_fingerprint_randomize.sh)
#   3. Session up + adb connected (scripts/vm2_start_display.sh)
#   4. APK splits copied to ~/ttapk/ — from your laptop:
#        scp -i ~/.ssh/jamescvermont /home/james/direct_api/ttapk/*.apk \
#            jamescvermont@<VM2_IP>:~/ttapk/
#
# Idempotent: skips install/push if already present.

set -euo pipefail

log() { printf '\n\033[1;34m[vm2_install_tt]\033[0m %s\n' "$*"; }

TTAPK_DIR="$HOME/ttapk"
FRIDA_VERSION=16.6.6
FRIDA_TARBALL="frida-server-${FRIDA_VERSION}-android-arm64.xz"
FRIDA_URL="https://github.com/frida/frida/releases/download/${FRIDA_VERSION}/${FRIDA_TARBALL}"

# ---------- sanity ----------
if ! adb devices | grep -q '127.0.0.1:5556.*device'; then
    echo "ERROR: adb not connected to 127.0.0.1:5556. Run scripts/vm2_start_display.sh first." >&2
    exit 1
fi
if ! ls "$TTAPK_DIR"/*.apk >/dev/null 2>&1; then
    cat <<EOF >&2
ERROR: no APK splits found at $TTAPK_DIR/*.apk

Copy them from your laptop first:
  scp -i ~/.ssh/jamescvermont /home/james/direct_api/ttapk/*.apk \\
      $(whoami)@\$(curl -s ifconfig.me):~/ttapk/
EOF
    exit 1
fi

# ---------- 1. TT Lite splits ----------
if adb -s 127.0.0.1:5556 shell pm path com.tiktok.lite.go 2>/dev/null | grep -q '^package:'; then
    log "TT Lite already installed — skipping install-multiple"
    adb -s 127.0.0.1:5556 shell dumpsys package com.tiktok.lite.go | grep -E "versionCode|versionName" | head -2
else
    log "installing TT Lite splits (10 APKs)"
    cd "$TTAPK_DIR"
    # Prefer globbing so we pick up whatever splits are present; adb
    # install-multiple tolerates order.
    adb -s 127.0.0.1:5556 install-multiple *.apk

    # Verify
    adb -s 127.0.0.1:5556 shell pm path com.tiktok.lite.go | head -5
    adb -s 127.0.0.1:5556 shell dumpsys package com.tiktok.lite.go | grep -E "versionCode|versionName" | head -2
fi

# ---------- 2. frida-server 16.6.6 ----------
# Note: 17.x crashes at startup on Android 13 per HISTORY — must use 16.6.6.
FRIDA_LOCAL="/tmp/frida-server-${FRIDA_VERSION}"
if [ ! -f "$FRIDA_LOCAL" ]; then
    log "downloading $FRIDA_TARBALL"
    cd /tmp
    if [ ! -f "$FRIDA_TARBALL" ]; then
        wget -q "$FRIDA_URL"
    fi
    unxz -k "$FRIDA_TARBALL"
    mv "frida-server-${FRIDA_VERSION}-android-arm64" "$FRIDA_LOCAL"
fi

log "pushing frida-server to container"
adb -s 127.0.0.1:5556 push "$FRIDA_LOCAL" /data/local/tmp/frida-server
adb -s 127.0.0.1:5556 shell chmod 755 /data/local/tmp/frida-server

# Kill any existing frida-server inside the container.
sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- pkill -f frida-server 2>/dev/null || true
sleep 1

log "starting frida-server (listens on 0.0.0.0:27042 inside container)"
sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- \
    sh -c 'nohup /data/local/tmp/frida-server -l 0.0.0.0:27042 >/tmp/frida-server.log 2>&1 &'
sleep 2

# Verify it's running.
if sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- pidof frida-server >/dev/null; then
    log "frida-server is running: pid=$(sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- pidof frida-server)"
else
    echo "WARNING: frida-server not detected via pidof. Check /tmp/frida-server.log inside container." >&2
fi

# ---------- 3. fingerprint verification ----------
log "post-install fingerprint check (these go into every TT signature):"
adb -s 127.0.0.1:5556 shell '
echo "ro.product.model      = $(getprop ro.product.model)"
echo "ro.product.manufacturer = $(getprop ro.product.manufacturer)"
echo "ro.product.brand      = $(getprop ro.product.brand)"
echo "ro.build.fingerprint  = $(getprop ro.build.fingerprint)"
echo "ro.serialno           = $(getprop ro.serialno)"
echo "ro.board.platform     = $(getprop ro.board.platform)"
'
echo "android_id = $(adb -s 127.0.0.1:5556 shell settings get secure android_id)"
echo "device_name = $(adb -s 127.0.0.1:5556 shell settings get global device_name)"

log "done. Next:"
cat <<'NEXT'
  1. Launch TT Lite via VNC (NOT via adb am start) so full registration runs.
     Complete signup for account-2 inside the UI.
  2. After signup + a few likes/searches, capture the session:
       bash scripts/vm2_capture_session.sh
NEXT
