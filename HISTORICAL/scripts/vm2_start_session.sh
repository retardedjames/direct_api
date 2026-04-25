#!/usr/bin/env bash
# Start a Waydroid session + UI on VM-2 and adb-connect.
# Idempotent: safe to re-run if the session has already started.
# Run as the login user (needs XDG_RUNTIME_DIR).

set -euo pipefail

log() { printf '\n\033[1;34m[vm2_start_session]\033[0m %s\n' "$*"; }

if [ "$EUID" -eq 0 ]; then
    echo "ERROR: run as the login user, not root. The Waydroid session is per-user." >&2
    exit 1
fi

# Ensure container is up (we can't start a session without it).
if ! sudo waydroid status 2>/dev/null | grep -q 'Container.*RUNNING'; then
    log "starting waydroid-container"
    sudo systemctl start waydroid-container
    sleep 3
fi

# Session already running?
if sudo waydroid status 2>/dev/null | grep -q 'Session.*RUNNING'; then
    log "session already RUNNING — skipping show-full-ui"
else
    log "bringing up UI in background (logs to /tmp/waydroid_ui.log)"
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    mkdir -p "$XDG_RUNTIME_DIR" 2>/dev/null || true
    # show-full-ui needs a Wayland display; on a headless VM we wrap it in an
    # Xvfb-backed weston-like stack. For first bring-up we just let it try
    # without a display — the container still boots, adb works, and VNC can
    # be layered on separately when we need the UI.
    nohup waydroid show-full-ui > /tmp/waydroid_ui.log 2>&1 &
    sleep 8
fi

# Wait for adbd inside the container to be ready.
log "adb connect 127.0.0.1:5556"
adb kill-server >/dev/null 2>&1 || true
for i in $(seq 1 20); do
    adb connect 127.0.0.1:5556 >/dev/null 2>&1 || true
    if adb -s 127.0.0.1:5556 shell getprop sys.boot_completed 2>/dev/null | grep -q '^1'; then
        log "boot_completed=1, device ready"
        break
    fi
    sleep 3
done

adb -s 127.0.0.1:5556 shell getprop ro.build.version.release || {
    echo "ERROR: adb could not reach the Android container" >&2
    exit 1
}

log "session is up. Device visible as 127.0.0.1:5556 in 'adb devices'."
adb devices
