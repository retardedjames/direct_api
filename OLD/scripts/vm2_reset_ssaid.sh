#!/usr/bin/env bash
# Reset the Android per-package SSAID database so TT Lite re-registers
# with a fresh openudid (and therefore fresh server-issued device_id +
# install_id).
#
# WHY THIS EXISTS:
#   ByteDance's openudid is derived from Settings.Secure.ANDROID_ID, which
#   on Android 8+ is a PER-APP value stored in
#   /data/system/users/0/settings_ssaid.xml. The whole table is HMAC'd
#   from a `userkey` row at the top of that file.
#
#   GCP machine images carry that file forward. Every clone of an image
#   inherits the same userkey AND the same com.tiktok.lite.go SSAID row.
#   pm clear does NOT touch system-level files. Result: vm3, vm4, vm5
#   all returned openudid=a6eba2dceadf37e7 to ByteDance, which deduped
#   them to the same device_id+install_id server-side. Fleet-wide tell.
#
#   This script deletes settings_ssaid.xml so Android regenerates it
#   from scratch on the next boot — fresh userkey, fresh per-app SSAID
#   for every package.
#
# WHEN TO RUN:
#   On every clone, BEFORE vm2_install_tt.sh / before TT Lite is ever
#   launched. Order in CLONE_SETUP.md Phase 2 is:
#     vm2_fingerprint_randomize.sh   (LXC MAC, persona, serialno)
#     vm2_reset_ssaid.sh             (this script — fresh per-app SSAID)
#     vm2_start_display.sh           (boots container; SSAID gets regenerated here)
#     vm2_apply_runtime_identity.sh  (android_id global, device_name, bt_mac)
#     pm clear com.tiktok.lite.go    (wipe app state — leaves SSAID intact)
#     vm2_install_tt.sh              (push frida-server)
#
# IDEMPOTENT: safe to re-run. Stops the container if it's running.

set -euo pipefail

log() { printf '\n\033[1;34m[vm2_ssaid]\033[0m %s\n' "$*"; }

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: must run as root (sudo bash $0)" >&2
    exit 1
fi

# Find data partition (bind mount source for /data inside the LXC).
SESSION_CFG=/var/lib/waydroid/lxc/waydroid/config_session
if [ ! -f "$SESSION_CFG" ]; then
    echo "ERROR: $SESSION_CFG missing. Has waydroid ever been started?" >&2
    exit 1
fi
DATA_DIR=$(awk '$1=="lxc.mount.entry" && $4=="data" {print $3; exit}' "$SESSION_CFG")
if [ -z "$DATA_DIR" ] || [ ! -d "$DATA_DIR" ]; then
    echo "ERROR: could not resolve waydroid data dir from $SESSION_CFG" >&2
    exit 1
fi
SSAID="$DATA_DIR/system/users/0/settings_ssaid.xml"
SSAID_FALLBACK="$DATA_DIR/system/users/0/settings_ssaid.xml.fallback"

log "data dir: $DATA_DIR"
log "target:   $SSAID"

# Container must be stopped — file is held / re-written on session stop.
log "stopping waydroid (will be restarted by vm2_start_display.sh)"
waydroid session stop 2>/dev/null || true
sleep 2
systemctl stop waydroid-container 2>/dev/null || true
sleep 1

if [ ! -f "$SSAID" ]; then
    log "no settings_ssaid.xml present — nothing to reset"
else
    OLD_USERKEY=$(strings "$SSAID" 2>/dev/null | grep -oE '[0-9A-F]{64}' | head -1 || true)
    log "current userkey hash (first 64 hex chars): ${OLD_USERKEY:-<unparsed>}"
    rm -f "$SSAID" "$SSAID_FALLBACK"
    log "deleted. Android will regenerate with a fresh userkey on next boot."
fi

# Belt + suspenders: also wipe TT Lite's data dir on the host. pm clear
# does this when run live; doing it here too means we don't depend on
# remembering pm clear after the next boot.
TT_DATA="$DATA_DIR/data/com.tiktok.lite.go"
TT_USER="$DATA_DIR/user/0/com.tiktok.lite.go"
for d in "$TT_DATA" "$TT_USER"; do
    if [ -d "$d" ]; then
        log "wiping app data: $d"
        rm -rf "$d"
    fi
done

# Restart the container so the next script in the chain can adb to it.
log "starting waydroid-container"
systemctl start waydroid-container
sleep 3

# Sanity: confirm SSAID is gone (it'll be regenerated when Android boots
# during vm2_start_display.sh, with a fresh userkey).
if [ -f "$SSAID" ]; then
    log "WARNING: settings_ssaid.xml reappeared before session start — "
    log "         this can happen if waydroid-container's init regenerated "
    log "         it. Verify with: strings $SSAID | grep com.tiktok"
else
    log "settings_ssaid.xml absent; will be regenerated fresh."
fi

log "done. Next: scripts/vm2_start_display.sh"
