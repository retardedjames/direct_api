#!/usr/bin/env bash
# clone_bootstrap.sh — bring a fresh GCP clone from "just booted" to
# "TT Lite on screen, ready for VNC signup". Runs on the new VM.
#
# Idempotent: stops + restarts services, re-randomizes everything.
# Order is load-bearing — the SSAID reset must happen with the container
# stopped, before TT Lite ever launches.
#
# Usage on the new VM:
#   bash ~/direct_api/scripts/clone_bootstrap.sh
#
# Then:
#   1. SSH-tunnel VNC (laptop): ssh -L 5901:localhost:5901 ... <NEW_IP>
#   2. Connect VNC client to localhost:5901, sign up TikTok, warm ~10 min.
#   3. On the new VM: bash ~/direct_api/scripts/clone_finalize.sh

set -euo pipefail

log() { printf '\n\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
err() { printf '\n\033[1;31m[bootstrap ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

REPO=$HOME/direct_api
[ -d "$REPO/scripts" ] || err "expected $REPO/scripts to exist (clone the repo first)"
cd "$REPO"

# 1. Persistent fingerprint: LXC MAC, persona, ro.serialno, build.prop.
log "[1/6] randomizing persistent fingerprint (container will stop+start)"
sudo bash scripts/vm2_fingerprint_randomize.sh

# 2. Reset per-package SSAID database. The image's userkey + every per-app
#    SSAID get carried forward by a GCP image clone; pm clear does not
#    touch them. Without this, ByteDance sees the same openudid every
#    clone, and registration server-dedupes us to the same device_id.
#    Container must be stopped (script handles it).
log "[2/6] wiping per-package SSAID DB"
sudo bash scripts/vm2_reset_ssaid.sh

# 3. Display stack: Xvfb + weston + waydroid session + x11vnc + adb forward.
#    Android boots here; settings_ssaid.xml is regenerated with a fresh
#    userkey on this boot.
log "[3/6] starting display stack + waydroid session"
bash scripts/vm2_start_display.sh

# 4. Runtime identity: bluetooth_address (real fingerprint input) +
#    device_name (cosmetic). NB: settings put secure android_id is a
#    no-op for fingerprinting — apps read the per-app SSAID, not the
#    global value. We still call it for completeness.
log "[4/6] applying runtime identity (bluetooth_address, device_name)"
bash scripts/vm2_apply_runtime_identity.sh

# 5. Belt-and-suspenders: pm clear TT Lite. Step 2 already wiped
#    /data/data/com.tiktok.lite.go on the host side, but pm clear is the
#    in-container path and is harmless if already empty.
log "[5/6] pm clear TT Lite (idempotent)"
adb -s 127.0.0.1:5556 shell pm clear com.tiktok.lite.go || true

# 6. Push frida-server (TT Lite already installed from the image).
#    Then launch TT Lite via adb so it's on screen for the VNC connect.
log "[6/6] pushing frida-server + launching TT Lite"
bash scripts/vm2_install_tt.sh
adb -s 127.0.0.1:5556 shell am start \
    -n com.tiktok.lite.go/com.ss.android.ugc.aweme.main.homepage.MainActivity \
    >/dev/null

# x11vnc can die when the SSH session that started it exits. Relaunch
# detached so VNC is reliably available after this script returns.
log "respawning x11vnc detached"
pkill -u "$(id -u)" -x x11vnc 2>/dev/null || true
mkdir -p "$HOME/logs"
nohup x11vnc -display :1 -forever -nopw -localhost -shared -rfbport 5901 \
    > "$HOME/logs/x11vnc.log" 2>&1 &
disown

# Kill SystemUI once so its InputDispatcher restarts cleanly. Without
# this, every tap in VNC triggers an "isn't responding" ANR. Plain
# adb-shell kill can't signal the system UID, so do it via lxc-attach.
log "killing SystemUI for clean VNC InputDispatcher"
SYSPID=$(adb -s 127.0.0.1:5556 shell pidof com.android.systemui 2>/dev/null | tr -d '\r' || true)
if [ -n "$SYSPID" ]; then
    sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- kill -9 "$SYSPID" || true
    sleep 3
fi

# Wait for ByteDance device registration to settle, then read out the
# IDs that TT Lite ended up with. The IMAGE shipped with a known
# openudid; the new clone's openudid MUST be different. If they match,
# the SSAID reset didn't take and we should not proceed.
log "waiting 30s for TT Lite to register with ByteDance"
sleep 30

OPENUDID=$(sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- \
    grep -oE 'openudid&quot;:&quot;[^&]*' \
    /data/data/com.tiktok.lite.go/shared_prefs/push_multi_process_config.xml \
    2>/dev/null | head -1 | sed 's/.*&quot;//' || true)
DEVICE_ID=$(sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- \
    grep -oE '<string name="device_id">[^<]*' \
    /data/data/com.tiktok.lite.go/shared_prefs/applog_stats.xml \
    2>/dev/null | sed 's/.*>//' || true)
INSTALL_ID=$(sudo lxc-attach -P /var/lib/waydroid/lxc -n waydroid -- \
    grep -oE '<string name="install_id">[^<]*' \
    /data/data/com.tiktok.lite.go/shared_prefs/applog_stats.xml \
    2>/dev/null | sed 's/.*>//' || true)

cat <<SUMMARY

\033[1;32m=== bootstrap complete ===\033[0m
external IP:     $(curl -s ifconfig.me 2>/dev/null || echo "?")
model:           $(adb -s 127.0.0.1:5556 shell getprop ro.product.model 2>/dev/null | tr -d '\r')
serial:          $(adb -s 127.0.0.1:5556 shell getprop ro.serialno 2>/dev/null | tr -d '\r')
eth0 MAC:        $(adb -s 127.0.0.1:5556 shell ip link show eth0 2>/dev/null | grep -oE 'ether [0-9a-f:]+' | awk '{print $2}')
TT Lite pid:     $(adb -s 127.0.0.1:5556 shell pidof com.tiktok.lite.go 2>/dev/null | tr -d '\r')

ByteDance-issued (must differ from the source image's values):
  openudid:    $OPENUDID
  device_id:   $DEVICE_ID
  install_id:  $INSTALL_ID

\033[1;33mNext (on your laptop):\033[0m
  ssh -L 5901:localhost:5901 -i ~/.ssh/jamescvermont jamescvermont@<this-vm-ip>
  # then VNC client → localhost:5901, sign up TikTok, warm ~10 min.

\033[1;33mThen back on this VM:\033[0m
  bash ~/direct_api/scripts/clone_finalize.sh
SUMMARY
