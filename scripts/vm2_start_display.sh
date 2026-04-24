#!/usr/bin/env bash
# Bring up the headless display stack needed for a Waydroid session on VM-2:
#
#   Xvfb   :1       — virtual X server backing the whole stack
#   weston --backend=x11   — Wayland compositor that renders into Xvfb
#   waydroid session start + show-full-ui — Android attaches to weston
#   x11vnc -display :1      — exposes the Xvfb display for SSH-tunneled VNC
#
# Once running, connect from your laptop with:
#   ssh -L 5901:localhost:5901 -i ~/.ssh/jamescvermont jamescvermont@<VM2_IP>
# then point a VNC client at localhost:5901 (no password).
#
# Run as the login user (needs XDG_RUNTIME_DIR). Safe to re-run — each
# component is killed first if already up.

set -euo pipefail

log() { printf '\n\033[1;34m[vm2_display]\033[0m %s\n' "$*"; }

if [ "$EUID" -eq 0 ]; then
    echo "ERROR: run as the login user, not root." >&2
    exit 1
fi

# Ensure container is up (we need it running before session start).
if ! systemctl is-active --quiet waydroid-container; then
    log "starting waydroid-container"
    sudo systemctl start waydroid-container
    sleep 3
fi

# ---------- paths / env ----------
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
export DISPLAY=:1
export WAYLAND_DISPLAY=wayland-0
LOGDIR=$HOME/logs
mkdir -p "$LOGDIR"

# ---------- kill previous processes ----------
for p in x11vnc weston Xvfb; do
    if pgrep -u "$(id -u)" -x "$p" >/dev/null 2>&1; then
        pkill -u "$(id -u)" -x "$p" || true
    fi
done
sleep 1

# Stop any prior waydroid session — a stale session blocks show-full-ui.
waydroid session stop 2>/dev/null || true
sleep 1

# ---------- 1. Xvfb ----------
# Portrait phone-ish resolution. 1080x2160 matches a Galaxy A54.
log "starting Xvfb :1 (1080x2160)"
Xvfb :1 -screen 0 1080x2160x24 -nolisten tcp > "$LOGDIR/xvfb.log" 2>&1 &
XVFB_PID=$!
sleep 1
if ! kill -0 $XVFB_PID 2>/dev/null; then
    echo "ERROR: Xvfb failed to start. Tail:" >&2
    tail -20 "$LOGDIR/xvfb.log" >&2
    exit 1
fi

# ---------- 2. openbox (window manager for Xvfb) ----------
# Xvfb by itself has no window manager; weston's X11 window needs one to be
# positioned cleanly for VNC viewing. openbox is lightweight.
log "starting openbox on :1"
DISPLAY=:1 openbox > "$LOGDIR/openbox.log" 2>&1 &
sleep 1

# ---------- 3. weston (Wayland compositor on X11 backend) ----------
# --backend=x11 makes weston open a window on Xvfb and provide a Wayland
# socket for clients (Waydroid) to draw into.
log "starting weston --backend=x11"
weston --backend=x11 --width=1080 --height=2160 --shell=desktop > "$LOGDIR/weston.log" 2>&1 &
WESTON_PID=$!
# Weston takes ~2s to create the socket.
for i in $(seq 1 10); do
    if [ -S "$XDG_RUNTIME_DIR/wayland-0" ] || [ -S "$XDG_RUNTIME_DIR/wayland-1" ]; then
        break
    fi
    sleep 1
done
if ! ls "$XDG_RUNTIME_DIR"/wayland-* >/dev/null 2>&1; then
    echo "ERROR: weston did not create a Wayland socket. Tail:" >&2
    tail -30 "$LOGDIR/weston.log" >&2
    exit 1
fi
# Use whichever socket weston created.
SOCK=$(ls "$XDG_RUNTIME_DIR"/wayland-* | head -1)
export WAYLAND_DISPLAY=$(basename "$SOCK")
log "wayland socket: $SOCK"

# ---------- 4. x11vnc ----------
# -forever: keep running after a client disconnects.
# -nopw -localhost: no password, SSH-tunnel only (safe).
# -shared: allow multiple simultaneous clients.
log "starting x11vnc on localhost:5901"
x11vnc -display :1 -forever -nopw -localhost -shared -rfbport 5901 \
    > "$LOGDIR/x11vnc.log" 2>&1 &
sleep 1

# ---------- 5. waydroid session ----------
log "starting waydroid session (attaching to weston)"
nohup waydroid session start > "$LOGDIR/waydroid_session.log" 2>&1 &
sleep 5
nohup waydroid show-full-ui > "$LOGDIR/waydroid_ui.log" 2>&1 &

# Wait for the Android container to be boot_completed.
log "waiting for Android to finish booting"
adb kill-server >/dev/null 2>&1 || true
BOOTED=0
for i in $(seq 1 60); do
    adb connect 127.0.0.1:5556 >/dev/null 2>&1 || true
    if adb -s 127.0.0.1:5556 shell getprop sys.boot_completed 2>/dev/null | grep -q '^1'; then
        BOOTED=1
        break
    fi
    sleep 3
done
if [ "$BOOTED" -ne 1 ]; then
    echo "ERROR: boot_completed=1 never observed. Logs in $LOGDIR/." >&2
    exit 1
fi

log "Android booted. Verification:"
adb -s 127.0.0.1:5556 shell 'echo "ro.product.model=$(getprop ro.product.model)"; echo "ro.product.manufacturer=$(getprop ro.product.manufacturer)"; echo "ro.build.fingerprint=$(getprop ro.build.fingerprint)"; echo "ro.serialno=$(getprop ro.serialno)"'

cat <<NEXT

\033[1;32mDisplay stack is up.\033[0m

From your laptop (replace VM2_IP):
  ssh -L 5901:localhost:5901 -i ~/.ssh/jamescvermont jamescvermont@<VM2_IP>
Then point a VNC client (TigerVNC, RealVNC, macOS Screen Sharing) at:
  localhost:5901

Logs: $LOGDIR/
  xvfb.log, openbox.log, weston.log, x11vnc.log, waydroid_session.log, waydroid_ui.log

Next:
  scripts/vm2_apply_runtime_identity.sh   # android_id / device_name / bluetooth_address
  scripts/vm2_install_tt.sh               # TT Lite splits + frida-server

NEXT
